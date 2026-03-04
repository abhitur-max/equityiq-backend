"""
EquityIQ Backend Server
Fetches data from BSE XBRL, NSE, and SEC EDGAR
Deploy free on Render.com
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx
import asyncio
import json
import re
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="EquityIQ Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── In-memory cache ──────────────────────────────────────────────────────────
cache: Dict[str, Any] = {}
CACHE_TTL_HOURS = 6

def cache_get(key: str):
    if key in cache:
        entry = cache[key]
        if datetime.now() - entry["ts"] < timedelta(hours=CACHE_TTL_HOURS):
            return entry["data"]
        del cache[key]
    return None

def cache_set(key: str, data: Any):
    cache[key] = {"data": data, "ts": datetime.now()}

# ─── BSE Helpers ─────────────────────────────────────────────────────────────
BSE_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_SEARCH = "https://api.bseindia.com/BseIndiaAPI/api/Search/w"
BSE_ANNOUNCEMENTS = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com"
}

SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {
    "User-Agent": "EquityIQ Research Terminal contact@equityiq.app",
    "Accept": "application/json"
}

async def fetch(client, url, headers=None, params=None):
    try:
        r = await client.get(url, headers=headers or HEADERS, params=params, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Fetch error {url}: {e}")
        return None

async def fetch_text(client, url, headers=None):
    try:
        r = await client.get(url, headers=headers or HEADERS, timeout=15.0)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"Fetch text error {url}: {e}")
        return ""

# ═══════════════════════════════════════════════════════════════════════════════
# AI PROXY ENDPOINT — This is the key addition
# ═══════════════════════════════════════════════════════════════════════════════
class AIRequest(BaseModel):
    messages: list
    system: str = ""
    max_tokens: int = 4000
    model: str = "claude-sonnet-4-20250514"

@app.post("/api/ai")
async def ai_proxy(req: AIRequest):
    """Proxy AI requests to Anthropic — avoids browser CORS restrictions"""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured on server. Add it in Render environment variables.")

    payload = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "messages": req.messages,
    }
    if req.system:
        payload["system"] = req.system

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return resp.json()

# ─── BSE: Find security code ─────────────────────────────────────────────────
async def bse_find_company(name, client):
    cached = cache_get(f"bse_company_{name}")
    if cached: return cached
    data = await fetch(client, BSE_SEARCH, params={"Type": "Q", "text": name})
    if not data: return None
    results = data.get("Matches", []) or data.get("matches", [])
    if not results: return None
    company = results[0]
    result = {
        "security_code": company.get("scrip_cd") or company.get("SCRIP_CD"),
        "name": company.get("long_name") or company.get("LONG_NAME") or name,
        "ticker": company.get("scrip_id") or company.get("SCRIP_ID"),
        "isin": company.get("isin_number") or company.get("ISIN_NUMBER"),
    }
    cache_set(f"bse_company_{name}", result)
    return result

# ─── Screener.in ─────────────────────────────────────────────────────────────
async def screener_get_financials(query, client):
    cached = cache_get(f"screener_{query}")
    if cached: return cached
    search = await fetch(client, f"https://www.screener.in/api/company/search/?q={query}&v=3&fts=1",
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    if not search: return None
    results = search if isinstance(search, list) else search.get("results", [])
    if not results: return None
    slug = results[0].get("url", "").strip("/").split("/")[-1]
    if not slug: return None
    html = await fetch_text(client, f"https://www.screener.in/company/{slug}/consolidated/",
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    if not html:
        html = await fetch_text(client, f"https://www.screener.in/company/{slug}/",
                                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    if not html: return None
    result = parse_screener_html(html)
    cache_set(f"screener_{query}", result)
    return result

def parse_screener_html(html):
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    def parse_table(section_id):
        section = soup.find("section", {"id": section_id})
        if not section: return []
        table = section.find("table")
        if not table: return []
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells and headers:
                rows.append(dict(zip(headers, cells)))
        return rows

    def clean_num(val):
        if not val: return None
        val = val.replace(",", "").replace("%", "").replace("₹", "").strip()
        try: return float(val)
        except: return None

    pl_rows = parse_table("profit-loss")
    if pl_rows:
        years = [k for k in (pl_rows[0].keys() if pl_rows else []) if k and k != ""]
        annual_pl = []
        for yr in years[1:4]:
            entry = {"year": yr}
            for row in pl_rows:
                label = list(row.values())[0].lower() if row else ""
                val = clean_num(row.get(yr, ""))
                if "sales" in label or "revenue" in label: entry["revenue"] = val
                elif "operating profit" in label or "ebitda" in label: entry["ebitda"] = val
                elif "net profit" in label: entry["netProfit"] = val
                elif "eps" in label: entry["eps"] = val
                elif "opm" in label: entry["opm"] = val
                elif "npm" in label: entry["npm"] = val
            annual_pl.append(entry)
        result["annualPL"] = annual_pl

    bs_rows = parse_table("balance-sheet")
    if bs_rows:
        years = [k for k in (bs_rows[0].keys() if bs_rows else []) if k and k != ""]
        annual_bs = []
        for yr in years[1:4]:
            entry = {"year": yr}
            for row in bs_rows:
                label = list(row.values())[0].lower() if row else ""
                val = clean_num(row.get(yr, ""))
                if "borrowings" in label or "debt" in label: entry["totalDebt"] = val
                elif "total assets" in label: entry["totalAssets"] = val
                elif "cash" in label: entry["cash"] = val
            annual_bs.append(entry)
        result["annualBS"] = annual_bs

    cf_rows = parse_table("cash-flow")
    if cf_rows:
        years = [k for k in (cf_rows[0].keys() if cf_rows else []) if k and k != ""]
        annual_cf = []
        for yr in years[1:4]:
            entry = {"year": yr}
            for row in cf_rows:
                label = list(row.values())[0].lower() if row else ""
                val = clean_num(row.get(yr, ""))
                if "operating" in label: entry["ocf"] = val
                elif "investing" in label: entry["investing"] = val
                elif "financing" in label: entry["financing"] = val
            if "ocf" in entry:
                capex = abs(entry.get("investing", 0) or 0)
                entry["capex"] = capex
                entry["fcf"] = (entry.get("ocf") or 0) - capex
            annual_cf.append(entry)
        result["annualCF"] = annual_cf

    ratios_rows = parse_table("ratios")
    if ratios_rows:
        ratios = {}
        years = [k for k in (ratios_rows[0].keys() if ratios_rows else []) if k and k != ""]
        latest_yr = years[1] if len(years) > 1 else None
        if latest_yr:
            for row in ratios_rows:
                label = list(row.values())[0].lower() if row else ""
                val = clean_num(row.get(latest_yr, ""))
                if "roe" in label: ratios["roe"] = val
                elif "roce" in label: ratios["roce"] = val
                elif "debt" in label and "equity" in label: ratios["deRatio"] = val
                elif "current ratio" in label: ratios["currentRatio"] = val
                elif "interest coverage" in label: ratios["interestCoverage"] = val
        result["ratios"] = ratios

    name_el = soup.find("h1", {"class": "company-name"}) or soup.find("h1")
    if name_el: result["company"] = name_el.get_text(strip=True)
    return result

# ─── BSE Disclosures ──────────────────────────────────────────────────────────
async def bse_get_disclosures(security_code, client):
    cached = cache_get(f"bse_disc_{security_code}")
    if cached: return cached
    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")
    disclosures = []
    ann_data = await fetch(client, BSE_ANNOUNCEMENTS, params={
        "pageno": 1, "strCat": "-1", "strPrevDate": from_date,
        "strScrip": security_code, "strSearch": "P",
        "strToDate": to_date, "strType": "C"
    })
    if ann_data:
        for item in ann_data.get("Table", [])[:20]:
            disc_type = item.get("CATEGORYNAME", "General")
            disclosures.append({
                "id": item.get("NEWSID", ""),
                "date": item.get("News_submission_dt", ""),
                "headline": item.get("NEWSSUB", ""),
                "type": disc_type,
                "category": categorise_disclosure(disc_type, item.get("NEWSSUB", "")),
                "pdf_url": f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{item.get('ATTACHMENTNAME', '')}",
                "source": "BSE"
            })
    seen = set()
    unique = [d for d in disclosures if d["id"] not in seen and not seen.add(d["id"])]
    unique.sort(key=lambda x: x.get("date", ""), reverse=True)
    cache_set(f"bse_disc_{security_code}", unique)
    return unique

def categorise_disclosure(disc_type, headline):
    combined = (disc_type + " " + headline).lower()
    if any(w in combined for w in ["result", "financial", "quarterly", "annual"]): return "results"
    if any(w in combined for w in ["dividend", "buyback", "bonus", "split"]): return "corporate_action"
    if any(w in combined for w in ["board meeting", "board outcome"]): return "board"
    if any(w in combined for w in ["regulation", "sebi", "penalty"]): return "regulatory"
    return "general"

# ─── SEC EDGAR ────────────────────────────────────────────────────────────────
async def sec_find_cik(name, client):
    cached = cache_get(f"sec_cik_{name}")
    if cached: return cached
    data = await fetch(client, SEC_COMPANY_TICKERS, headers=SEC_HEADERS)
    if not data: return None
    name_lower = name.lower()
    for key, company in data.items():
        if name_lower in company.get("title", "").lower() or name_lower in company.get("ticker", "").lower():
            cik = str(company["cik_str"]).zfill(10)
            cache_set(f"sec_cik_{name}", cik)
            return cik
    return None

async def sec_get_financials(cik, client):
    cached = cache_get(f"sec_fin_{cik}")
    if cached: return cached
    facts = await fetch(client, SEC_FACTS.format(cik=cik), headers=SEC_HEADERS)
    if not facts: return {}
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    def get_annual(concept, n=3):
        data = us_gaap.get(concept, {}).get("units", {})
        usd = data.get("USD", [])
        annual = [x for x in usd if x.get("form") == "10-K" and x.get("fp") == "FY"]
        annual.sort(key=lambda x: x.get("end", ""), reverse=True)
        return annual[:n]

    revenues_a = get_annual("Revenues") or get_annual("RevenueFromContractWithCustomerExcludingAssessedTax")
    net_income_a = get_annual("NetIncomeLoss")
    assets_a = get_annual("Assets")
    debt_a = get_annual("LongTermDebt")
    ocf_a = get_annual("NetCashProvidedByUsedInOperatingActivities")
    capex_a = get_annual("PaymentsToAcquirePropertyPlantAndEquipment")

    annual_pl = []
    for i in range(min(3, len(revenues_a))):
        rev = revenues_a[i].get("val")
        ni = net_income_a[i].get("val") if i < len(net_income_a) else None
        annual_pl.append({
            "year": revenues_a[i].get("end", "")[:4],
            "revenue": round(rev/1e6, 1) if rev else None,
            "netProfit": round(ni/1e6, 1) if ni else None,
            "npm": round(ni/rev*100, 1) if rev and ni else None,
        })

    annual_bs = []
    for i in range(min(3, len(assets_a))):
        ta = assets_a[i].get("val")
        debt = debt_a[i].get("val") if i < len(debt_a) else None
        annual_bs.append({
            "year": assets_a[i].get("end", "")[:4],
            "totalAssets": round(ta/1e6, 1) if ta else None,
            "totalDebt": round(debt/1e6, 1) if debt else None,
        })

    annual_cf = []
    for i in range(min(3, len(ocf_a))):
        ocf = ocf_a[i].get("val")
        capex = capex_a[i].get("val") if i < len(capex_a) else None
        annual_cf.append({
            "year": ocf_a[i].get("end", "")[:4],
            "ocf": round(ocf/1e6, 1) if ocf else None,
            "capex": round(capex/1e6, 1) if capex else None,
            "fcf": round((ocf-capex)/1e6, 1) if ocf and capex else None,
        })

    result = {"annualPL": annual_pl, "annualBS": annual_bs, "annualCF": annual_cf,
              "currency": "USD", "currencySymbol": "$"}
    cache_set(f"sec_fin_{cik}", result)
    return result

async def sec_get_disclosures(cik, client):
    cached = cache_get(f"sec_disc_{cik}")
    if cached: return cached
    submissions = await fetch(client, SEC_SUBMISSIONS.format(cik=cik), headers=SEC_HEADERS)
    if not submissions: return []
    filings = submissions.get("filings", {}).get("recent", {})
    forms = filings.get("form", [])
    dates = filings.get("filingDate", [])
    accessions = filings.get("accessionNumber", [])
    primary_docs = filings.get("primaryDocument", [])
    disclosures = []
    relevant_forms = {"10-K", "10-Q", "8-K", "DEF 14A"}
    for i in range(min(len(forms), 30)):
        if forms[i] in relevant_forms:
            acc = accessions[i].replace("-", "") if i < len(accessions) else ""
            cik_short = cik.lstrip("0")
            doc = primary_docs[i] if i < len(primary_docs) else ""
            disclosures.append({
                "date": dates[i] if i < len(dates) else "",
                "headline": f"{forms[i]} — {dates[i] if i < len(dates) else ''}",
                "type": forms[i],
                "category": "results" if forms[i] in ("10-K","10-Q") else "board",
                "pdf_url": f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc}/{doc}",
                "source": "SEC EDGAR"
            })
    cache_set(f"sec_disc_{cik}", disclosures)
    return disclosures

# ─── News ─────────────────────────────────────────────────────────────────────
async def fetch_news(company_name, ticker, client):
    cached = cache_get(f"news_{ticker}")
    if cached: return cached
    news = []
    query = f"{company_name} {ticker} stock"
    rss_url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}&hl=en-IN&gl=IN&ceid=IN:en"
    rss_text = await fetch_text(client, rss_url, headers={"User-Agent": "Mozilla/5.0"})
    if rss_text:
        try:
            root = ET.fromstring(rss_text)
            for item in root.findall(".//item")[:10]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                link = item.findtext("link", "")
                source_el = item.find("source")
                source = source_el.text if source_el is not None else "Google News"
                news.append({"headline": title, "source": source,
                             "date": pub_date[:16], "sentiment": "Neutral", "url": link})
        except ET.ParseError:
            pass
    cache_set(f"news_{ticker}", news)
    return news

# ─── Main endpoint ────────────────────────────────────────────────────────────
@app.get("/api/company/{name}")
async def get_company_data(name: str, country: str = ""):
    cached = cache_get(f"full_{name.upper()}")
    if cached:
        return JSONResponse(content={**cached, "fromCache": True})

    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = {
            "company": name, "ticker": name.upper(),
            "exchange": "NSE", "country": "India",
            "currency": "INR", "currencySymbol": "₹",
            "annualPL": [], "quarterlyPL": [],
            "annualBS": [], "annualCF": [],
            "ratios": {}, "disclosures": [], "news": [],
            "dataSource": "BSE/Screener.in",
            "fetchedAt": datetime.now().isoformat()
        }

        # Only check SEC for US companies (when country=US explicitly)
        sec_cik = None
        if country.upper() == "US":
            sec_cik = await sec_find_cik(name, client)

        if sec_cik:
            result.update({"country": "US", "currency": "USD", "currencySymbol": "$",
                           "exchange": "NASDAQ/NYSE", "dataSource": "SEC EDGAR"})
            fin = await sec_get_financials(sec_cik, client)
            result.update(fin)
            result["disclosures"] = await sec_get_disclosures(sec_cik, client)
            subs = await fetch(client, SEC_SUBMISSIONS.format(cik=sec_cik), headers=SEC_HEADERS)
            if subs:
                result["company"] = subs.get("name", name)
                result["ticker"] = (subs.get("tickers") or [name])[0]
                result["exchange"] = (subs.get("exchanges") or ["NYSE"])[0]
        else:
            # Indian stock
            bse_company = await bse_find_company(name, client)
            screener_data = await screener_get_financials(name, client)
            if screener_data:
                result.update(screener_data)
            if bse_company:
                result["ticker"] = bse_company.get("ticker", name)
                if bse_company.get("name"): result["company"] = bse_company["name"]
                if bse_company.get("security_code"):
                    result["disclosures"] = await bse_get_disclosures(bse_company["security_code"], client)

        result["news"] = await fetch_news(result["company"], result["ticker"], client)
        cache_set(f"full_{name.upper()}", result)
        return JSONResponse(content=result)

@app.get("/health")
async def health():
    return {"status": "ok", "ai_ready": bool(ANTHROPIC_API_KEY),
            "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {"message": "EquityIQ Backend API", "version": "1.0.0",
            "endpoints": ["/api/company/{name}", "/api/ai", "/health"]}
