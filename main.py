"""
EquityIQ Backend Server
Fetches data from BSE XBRL, NSE, and SEC EDGAR
Deploy free on Render.com
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
BSE_XBRL_LIST = "https://api.bseindia.com/BseIndiaAPI/api/XBRL/w"
BSE_ANNOUNCEMENTS = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_CORP_ACTION = "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com"
}

# ─── SEC EDGAR Helpers ───────────────────────────────────────────────────────
SEC_COMPANY_SEARCH = "https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt=2020-01-01&forms=10-K,10-Q"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{query}%22&forms=10-K,10-Q,8-K"
SEC_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"

SEC_HEADERS = {
    "User-Agent": "EquityIQ Research Terminal contact@equityiq.app",
    "Accept": "application/json"
}

async def fetch(client: httpx.AsyncClient, url: str, headers=None, params=None) -> Any:
    try:
        r = await client.get(url, headers=headers or HEADERS, params=params, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Fetch error {url}: {e}")
        return None

async def fetch_text(client: httpx.AsyncClient, url: str, headers=None) -> str:
    try:
        r = await client.get(url, headers=headers or HEADERS, timeout=15.0)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"Fetch text error {url}: {e}")
        return ""

# ─── BSE: Find security code ─────────────────────────────────────────────────
async def bse_find_company(name: str, client: httpx.AsyncClient) -> Optional[Dict]:
    cached = cache_get(f"bse_company_{name}")
    if cached: return cached

    data = await fetch(client, BSE_SEARCH, params={"Type": "Q", "text": name})
    if not data: return None

    results = data.get("Matches", []) or data.get("matches", [])
    if not results:
        # Try scraping BSE search page
        html = await fetch_text(client, f"https://www.bseindia.com/corporates/List_Scrips.html")
        return None

    company = results[0]
    result = {
        "security_code": company.get("scrip_cd") or company.get("SCRIP_CD"),
        "name": company.get("long_name") or company.get("LONG_NAME") or name,
        "ticker": company.get("scrip_id") or company.get("SCRIP_ID"),
        "isin": company.get("isin_number") or company.get("ISIN_NUMBER"),
    }
    cache_set(f"bse_company_{name}", result)
    return result

# ─── BSE: Get XBRL financial data ────────────────────────────────────────────
async def bse_get_financials(security_code: str, client: httpx.AsyncClient) -> Dict:
    cached = cache_get(f"bse_fin_{security_code}")
    if cached: return cached

    # Get quarterly results from BSE
    quarterly_url = f"https://api.bseindia.com/BseIndiaAPI/api/StockReachGraph/w"
    params = {"scripcode": security_code, "flag": "Q", "fromdate": "", "todate": "", "seriesid": ""}

    fin_data = {"annual": [], "quarterly": [], "balance_sheet": [], "cash_flow": []}

    # Fetch financial results
    results_url = f"https://api.bseindia.com/BseIndiaAPI/api/FinancialResults/w"
    annual = await fetch(client, results_url, params={"scripcode": security_code, "period": "Annual"})
    quarterly = await fetch(client, results_url, params={"scripcode": security_code, "period": "Quarterly"})

    if annual:
        fin_data["annual"] = annual.get("Table", [])[:3]  # last 3 years
    if quarterly:
        fin_data["quarterly"] = quarterly.get("Table", [])[:4]  # last 4 quarters

    # Try Screener.in as reliable fallback for structured data
    screener_data = await screener_get_financials(security_code, client)
    if screener_data:
        fin_data.update(screener_data)

    cache_set(f"bse_fin_{security_code}", fin_data)
    return fin_data

# ─── Screener.in: Get structured financials ──────────────────────────────────
async def screener_get_financials(query: str, client: httpx.AsyncClient) -> Optional[Dict]:
    cached = cache_get(f"screener_{query}")
    if cached: return cached

    # Search for company on Screener
    search = await fetch(client, f"https://www.screener.in/api/company/search/?q={query}&v=3&fts=1",
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    if not search or not search: return None

    results = search if isinstance(search, list) else search.get("results", [])
    if not results: return None

    slug = results[0].get("url", "").strip("/").split("/")[-1]
    if not slug: return None

    # Fetch company page
    html = await fetch_text(client, f"https://www.screener.in/company/{slug}/consolidated/",
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    if not html:
        html = await fetch_text(client, f"https://www.screener.in/company/{slug}/",
                                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    if not html: return None

    return parse_screener_html(html)

def parse_screener_html(html: str) -> Dict:
    """Parse Screener.in HTML to extract financial tables"""
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    def parse_table(section_id: str) -> List[Dict]:
        section = soup.find("section", {"id": section_id})
        if not section: return []
        table = section.find("table")
        if not table: return []

        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells and headers:
                row = dict(zip(headers, cells))
                rows.append(row)
        return rows

    def clean_num(val: str) -> Optional[float]:
        if not val: return None
        val = val.replace(",", "").replace("%", "").replace("₹", "").strip()
        try: return float(val)
        except: return None

    # Parse P&L
    pl_rows = parse_table("profit-loss")
    if pl_rows:
        years = [k for k in (pl_rows[0].keys() if pl_rows else []) if k and k != ""]
        annual_pl = []
        for yr in years[1:4]:  # Last 3 years
            entry = {"year": yr}
            for row in pl_rows:
                label = list(row.values())[0].lower() if row else ""
                val = clean_num(row.get(yr, ""))
                if "sales" in label or "revenue" in label: entry["revenue"] = val
                elif "expenses" in label: entry["expenses"] = val
                elif "operating profit" in label or "ebitda" in label: entry["ebitda"] = val
                elif "net profit" in label: entry["netProfit"] = val
                elif "eps" in label: entry["eps"] = val
                elif "opm" in label: entry["opm"] = val
                elif "npm" in label: entry["npm"] = val
            annual_pl.append(entry)
        result["annualPL"] = annual_pl

    # Parse Balance Sheet
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
                elif "reserves" in label: entry["reserves"] = val
                elif "total assets" in label: entry["totalAssets"] = val
                elif "cash" in label: entry["cash"] = val
                elif "investments" in label: entry["investments"] = val
            annual_bs.append(entry)
        result["annualBS"] = annual_bs

    # Parse Cash Flow
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
            if "ocf" in entry and "investing" in entry:
                capex = abs(entry.get("investing", 0) or 0)
                entry["capex"] = capex
                entry["fcf"] = (entry.get("ocf") or 0) - capex
            annual_cf.append(entry)
        result["annualCF"] = annual_cf

    # Parse Ratios
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

    # Company name & basics
    name_el = soup.find("h1", {"class": "company-name"}) or soup.find("h1")
    if name_el: result["company"] = name_el.get_text(strip=True)

    return result

# ─── BSE: Get Announcements & Disclosures ─────────────────────────────────────
async def bse_get_disclosures(security_code: str, client: httpx.AsyncClient) -> List[Dict]:
    cached = cache_get(f"bse_disc_{security_code}")
    if cached: return cached

    to_date = datetime.now().strftime("%Y%m%d")
    from_date = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")

    disclosures = []

    # Fetch announcements
    ann_data = await fetch(client, BSE_ANNOUNCEMENTS, params={
        "pageno": 1, "strCat": "-1", "strPrevDate": from_date,
        "strScrip": security_code, "strSearch": "P",
        "strToDate": to_date, "strType": "C"
    })

    if ann_data:
        items = ann_data.get("Table", [])
        for item in items[:20]:
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

    # Fetch financial results specifically
    results_ann = await fetch(client, BSE_ANNOUNCEMENTS, params={
        "pageno": 1, "strCat": "Result", "strPrevDate": from_date,
        "strScrip": security_code, "strSearch": "P",
        "strToDate": to_date, "strType": "C"
    })

    if results_ann:
        for item in results_ann.get("Table", [])[:5]:
            disclosures.append({
                "id": item.get("NEWSID", ""),
                "date": item.get("News_submission_dt", ""),
                "headline": item.get("NEWSSUB", ""),
                "type": "Financial Results",
                "category": "results",
                "pdf_url": f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{item.get('ATTACHMENTNAME', '')}",
                "source": "BSE"
            })

    # Deduplicate and sort
    seen = set()
    unique = []
    for d in disclosures:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)

    unique.sort(key=lambda x: x.get("date", ""), reverse=True)
    cache_set(f"bse_disc_{security_code}", unique)
    return unique

def categorise_disclosure(disc_type: str, headline: str) -> str:
    combined = (disc_type + " " + headline).lower()
    if any(w in combined for w in ["result", "financial", "quarterly", "annual"]): return "results"
    if any(w in combined for w in ["dividend", "buyback", "bonus", "split"]): return "corporate_action"
    if any(w in combined for w in ["board meeting", "board outcome"]): return "board"
    if any(w in combined for w in ["investor presentation", "analyst"]): return "investor_relations"
    if any(w in combined for w in ["shareholding", "promoter"]): return "shareholding"
    if any(w in combined for w in ["rating", "credit"]): return "credit_rating"
    if any(w in combined for w in ["acquisition", "merger", "amalgamation"]): return "ma"
    if any(w in combined for w in ["regulation", "sebi", "penalty", "notice"]): return "regulatory"
    return "general"

# ─── SEC EDGAR: Find company CIK ─────────────────────────────────────────────
async def sec_find_cik(name: str, client: httpx.AsyncClient) -> Optional[str]:
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

# ─── SEC EDGAR: Get financial facts ──────────────────────────────────────────
async def sec_get_financials(cik: str, client: httpx.AsyncClient) -> Dict:
    cached = cache_get(f"sec_fin_{cik}")
    if cached: return cached

    facts = await fetch(client, SEC_FACTS.format(cik=cik), headers=SEC_HEADERS)
    if not facts: return {}

    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    def get_annual_values(concept: str, n: int = 3) -> List[Dict]:
        data = us_gaap.get(concept, {}).get("units", {})
        usd_data = data.get("USD", []) or data.get("shares", []) or data.get("pure", [])
        annual = [x for x in usd_data if x.get("form") in ("10-K",) and x.get("fp") == "FY"]
        annual.sort(key=lambda x: x.get("end", ""), reverse=True)
        return annual[:n]

    def get_quarterly_values(concept: str, n: int = 4) -> List[Dict]:
        data = us_gaap.get(concept, {}).get("units", {})
        usd_data = data.get("USD", []) or []
        quarterly = [x for x in usd_data if x.get("form") in ("10-Q",)]
        quarterly.sort(key=lambda x: x.get("end", ""), reverse=True)
        return quarterly[:n]

    def safe_val(entries: List[Dict], idx: int = 0) -> Optional[float]:
        if idx < len(entries):
            return entries[idx].get("val")
        return None

    # Extract key metrics
    revenues_a = get_annual_values("Revenues") or get_annual_values("RevenueFromContractWithCustomerExcludingAssessedTax")
    net_income_a = get_annual_values("NetIncomeLoss")
    assets_a = get_annual_values("Assets")
    debt_a = get_annual_values("LongTermDebt")
    ocf_a = get_annual_values("NetCashProvidedByUsedInOperatingActivities")
    capex_a = get_annual_values("PaymentsToAcquirePropertyPlantAndEquipment")

    revenues_q = get_quarterly_values("Revenues") or get_quarterly_values("RevenueFromContractWithCustomerExcludingAssessedTax")
    net_income_q = get_quarterly_values("NetIncomeLoss")

    # Build annual P&L
    annual_pl = []
    for i in range(min(3, len(revenues_a))):
        rev = safe_val(revenues_a, i)
        ni = safe_val(net_income_a, i)
        year = revenues_a[i].get("end", "")[:4] if i < len(revenues_a) else ""
        annual_pl.append({
            "year": year,
            "revenue": round(rev / 1e6, 1) if rev else None,  # Convert to millions
            "netProfit": round(ni / 1e6, 1) if ni else None,
            "npm": round(ni / rev * 100, 1) if rev and ni else None,
        })

    # Build quarterly P&L
    quarterly_pl = []
    for i in range(min(4, len(revenues_q))):
        rev = safe_val(revenues_q, i)
        ni = safe_val(net_income_q, i)
        period = revenues_q[i].get("end", "")[:7] if i < len(revenues_q) else ""
        quarterly_pl.append({
            "quarter": period,
            "revenue": round(rev / 1e6, 1) if rev else None,
            "netProfit": round(ni / 1e6, 1) if ni else None,
        })

    # Build Balance Sheet
    annual_bs = []
    for i in range(min(3, len(assets_a))):
        ta = safe_val(assets_a, i)
        debt = safe_val(debt_a, i)
        year = assets_a[i].get("end", "")[:4] if i < len(assets_a) else ""
        annual_bs.append({
            "year": year,
            "totalAssets": round(ta / 1e6, 1) if ta else None,
            "totalDebt": round(debt / 1e6, 1) if debt else None,
        })

    # Build Cash Flow
    annual_cf = []
    for i in range(min(3, len(ocf_a))):
        ocf = safe_val(ocf_a, i)
        capex = safe_val(capex_a, i)
        year = ocf_a[i].get("end", "")[:4] if i < len(ocf_a) else ""
        fcf = None
        if ocf and capex:
            fcf = round((ocf - capex) / 1e6, 1)
        annual_cf.append({
            "year": year,
            "ocf": round(ocf / 1e6, 1) if ocf else None,
            "capex": round(capex / 1e6, 1) if capex else None,
            "fcf": fcf,
        })

    result = {
        "annualPL": annual_pl,
        "quarterlyPL": quarterly_pl,
        "annualBS": annual_bs,
        "annualCF": annual_cf,
        "currency": "USD",
        "currencySymbol": "$",
        "unit": "USD Millions"
    }

    cache_set(f"sec_fin_{cik}", result)
    return result

# ─── SEC: Get filings/disclosures ─────────────────────────────────────────────
async def sec_get_disclosures(cik: str, client: httpx.AsyncClient) -> List[Dict]:
    cached = cache_get(f"sec_disc_{cik}")
    if cached: return cached

    submissions = await fetch(client, SEC_SUBMISSIONS.format(cik=cik), headers=SEC_HEADERS)
    if not submissions: return []

    filings = submissions.get("filings", {}).get("recent", {})
    forms = filings.get("form", [])
    dates = filings.get("filingDate", [])
    descriptions = filings.get("primaryDocument", [])
    accessions = filings.get("accessionNumber", [])

    disclosures = []
    relevant_forms = {"10-K", "10-Q", "8-K", "DEF 14A", "S-1"}

    for i in range(min(len(forms), 30)):
        form = forms[i]
        if form in relevant_forms:
            acc = accessions[i].replace("-", "") if i < len(accessions) else ""
            cik_short = cik.lstrip("0")
            pdf_url = f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc}/{descriptions[i] if i < len(descriptions) else ''}"
            disclosures.append({
                "date": dates[i] if i < len(dates) else "",
                "headline": form_to_headline(form, dates[i] if i < len(dates) else ""),
                "type": form,
                "category": form_to_category(form),
                "pdf_url": pdf_url,
                "source": "SEC EDGAR"
            })

    cache_set(f"sec_disc_{cik}", disclosures)
    return disclosures

def form_to_headline(form: str, date: str) -> str:
    mapping = {
        "10-K": f"Annual Report (10-K) filed {date}",
        "10-Q": f"Quarterly Report (10-Q) filed {date}",
        "8-K": f"Current Report (8-K) - Material Event {date}",
        "DEF 14A": f"Proxy Statement filed {date}",
        "S-1": f"IPO Registration Statement {date}",
    }
    return mapping.get(form, f"{form} filed {date}")

def form_to_category(form: str) -> str:
    mapping = {
        "10-K": "results", "10-Q": "results",
        "8-K": "board", "DEF 14A": "investor_relations", "S-1": "general"
    }
    return mapping.get(form, "general")

# ─── News fetcher ─────────────────────────────────────────────────────────────
async def fetch_news(company_name: str, ticker: str, country: str, client: httpx.AsyncClient) -> List[Dict]:
    cached = cache_get(f"news_{ticker}")
    if cached: return cached

    news = []

    # Google News RSS
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

                news.append({
                    "headline": title,
                    "source": source,
                    "date": format_date(pub_date),
                    "sentiment": "Neutral",
                    "url": link,
                    "type": "news"
                })
        except ET.ParseError:
            pass

    cache_set(f"news_{ticker}", news)
    return news

def format_date(date_str: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%b %d, %Y")
    except:
        return date_str[:16] if date_str else ""

# ─── Main company analyse endpoint ───────────────────────────────────────────
@app.get("/api/company/{name}")
async def get_company_data(name: str):
    """
    Main endpoint. Returns structured financial data for a company.
    Detects India vs US automatically, fetches from BSE/Screener or SEC EDGAR.
    """
    cached = cache_get(f"full_{name.upper()}")
    if cached:
        return JSONResponse(content={**cached, "fromCache": True})

    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = {
            "company": name,
            "ticker": name.upper(),
            "exchange": "NSE",
            "country": "India",
            "currency": "INR",
            "currencySymbol": "₹",
            "annualPL": [],
            "quarterlyPL": [],
            "annualBS": [],
            "annualCF": [],
            "ratios": {},
            "disclosures": [],
            "news": [],
            "dataSource": "BSE/Screener.in",
            "fetchedAt": datetime.now().isoformat()
        }

        # Detect if US stock (simple heuristic — check SEC first)
        sec_cik = await sec_find_cik(name, client)

        if sec_cik:
            # US Stock via SEC EDGAR
            logger.info(f"Found {name} on SEC EDGAR, CIK: {sec_cik}")
            result["country"] = "US"
            result["currency"] = "USD"
            result["currencySymbol"] = "$"
            result["exchange"] = "NASDAQ/NYSE"
            result["dataSource"] = "SEC EDGAR"

            fin = await sec_get_financials(sec_cik, client)
            result.update(fin)

            disclosures = await sec_get_disclosures(sec_cik, client)
            result["disclosures"] = disclosures

            subs = await fetch(client, SEC_SUBMISSIONS.format(cik=sec_cik), headers=SEC_HEADERS)
            if subs:
                result["company"] = subs.get("name", name)
                result["ticker"] = subs.get("tickers", [name])[0] if subs.get("tickers") else name
                result["exchange"] = subs.get("exchanges", ["NYSE"])[0] if subs.get("exchanges") else "NYSE"

        else:
            # Indian Stock via BSE + Screener.in
            logger.info(f"Searching {name} on BSE/Screener.in")
            bse_company = await bse_find_company(name, client)

            screener_data = await screener_get_financials(name, client)
            if screener_data:
                result.update(screener_data)

            if bse_company:
                result["ticker"] = bse_company.get("ticker", name)
                result["bse_code"] = bse_company.get("security_code")
                if bse_company.get("name"):
                    result["company"] = bse_company["name"]

                if bse_company.get("security_code"):
                    disclosures = await bse_get_disclosures(bse_company["security_code"], client)
                    result["disclosures"] = disclosures

        # Fetch news (works for both India & US)
        news = await fetch_news(result["company"], result["ticker"], result["country"], client)
        result["news"] = news

        cache_set(f"full_{name.upper()}", result)
        return JSONResponse(content=result)

# ─── Disclosures only endpoint ────────────────────────────────────────────────
@app.get("/api/disclosures/{security_code}")
async def get_disclosures(security_code: str):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        disclosures = await bse_get_disclosures(security_code, client)
        return {"disclosures": disclosures}

# ─── Latest filings check (for auto-refresh) ─────────────────────────────────
@app.get("/api/check-new-filings/{security_code}")
async def check_new_filings(security_code: str, since: Optional[str] = None):
    """Check if any new filings have appeared since last check"""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        disclosures = await bse_get_disclosures(security_code, client)
        if since:
            new = [d for d in disclosures if d.get("date", "") > since]
            return {"hasNew": len(new) > 0, "newCount": len(new), "newFilings": new}
        return {"disclosures": disclosures[:5]}

# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {"message": "EquityIQ Backend API", "version": "1.0.0",
            "endpoints": ["/api/company/{name}", "/api/disclosures/{bse_code}", "/health"]}
