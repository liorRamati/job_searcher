"""
Live scraper tests — verify each enabled company returns ≥ 1 job.

Run with:
    pytest -m smoke tests/test_scrapers_live.py -v

These tests call live APIs and are not run in CI.
Each test fetches the first page of jobs and asserts at least one came back.
"""

import pytest
import yaml


def load_enabled_companies():
    with open("config/companies.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [c for c in data["companies"] if c.get("enabled")]


ENABLED = load_enabled_companies()
COMPANY_IDS = [(c["name"], c["ats"], c.get("slug"), c.get("career_url")) for c in ENABLED]


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,slug",
    [(c["name"], c["slug"]) for c in ENABLED if c["ats"] == "greenhouse"],
)
def test_greenhouse_returns_jobs(name, slug):
    import requests
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        params={"content": "false"},
        timeout=15,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    jobs = r.json().get("jobs", [])
    assert len(jobs) >= 1, f"{name}: got 0 jobs from Greenhouse (slug={slug})"


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,slug",
    [(c["name"], c["slug"]) for c in ENABLED if c["ats"] == "lever"],
)
def test_lever_returns_jobs(name, slug):
    import requests
    r = requests.get(
        f"https://api.lever.co/v0/postings/{slug}?mode=json",
        timeout=15,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    jobs = r.json()
    assert isinstance(jobs, list), f"{name}: unexpected response type"
    assert len(jobs) >= 1, f"{name}: got 0 jobs from Lever (slug={slug})"


# ---------------------------------------------------------------------------
# Workday REST API
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,career_url",
    [(c["name"], c["career_url"]) for c in ENABLED if c["ats"] == "workday"],
)
def test_workday_returns_jobs(name, career_url):
    from scrapers.workday import _parse_career_url
    import requests

    base_url, tenant, wd_num, board, search_text = _parse_career_url(career_url)
    api_url = f"{base_url}/wday/cxs/{tenant}/{board}/jobs"

    body: dict = {"limit": 20, "offset": 0}
    if search_text:
        body["searchText"] = search_text
    r = requests.post(
        api_url,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0"},
        json=body,
        timeout=20,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code} from {api_url}"
    total = r.json().get("total", 0)
    assert total >= 1, f"{name}: total=0 from Workday API ({api_url})"


# ---------------------------------------------------------------------------
# Comeet API
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,slug",
    [(c["name"], c["slug"]) for c in ENABLED if c["ats"] == "comeet"],
)
def test_comeet_returns_jobs(name, slug):
    import requests
    company_uid, token = slug.split(":", 1)
    r = requests.get(
        f"https://www.comeet.co/careers-api/2.0/company/{company_uid}/positions",
        params={"token": token},
        timeout=15,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    jobs = r.json()
    assert isinstance(jobs, list), f"{name}: expected list"
    assert len(jobs) >= 1, f"{name}: got 0 jobs from Comeet (uid={company_uid})"


# ---------------------------------------------------------------------------
# Mobileye HTML scraper
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_mobileye_returns_jobs():
    import requests
    r = requests.get(
        "https://careers.mobileye.com/jobs",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    assert r.status_code == 200
    import re
    job_paths = list(set(re.findall(r'/jobs/([^/"\s]+)/([a-f0-9-]{36})', r.text)))
    assert len(job_paths) >= 1, "Mobileye: no jobs found in prerendered HTML"


# ---------------------------------------------------------------------------
# Varonis REST API
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_varonis_returns_jobs():
    import requests
    r = requests.get(
        "https://careers.varonis.com/api/getRequisitions",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://careers.varonis.com/"},
        timeout=15,
    )
    assert r.status_code == 200
    jobs = r.json().get("data", [])
    assert len(jobs) >= 1, "Varonis: got 0 jobs from API"


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,slug",
    [(c["name"], c["slug"]) for c in ENABLED if c["ats"] == "smartrecruiters"],
)
def test_smartrecruiters_returns_jobs(name, slug):
    import requests
    r = requests.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        params={"limit": 10},
        timeout=15,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    total = r.json().get("totalFound", 0)
    assert total >= 1, f"{name}: got 0 jobs from SmartRecruiters (slug={slug})"


# ---------------------------------------------------------------------------
# Eightfold.ai pcsx API
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,career_url",
    [(c["name"], c["career_url"]) for c in ENABLED if c["ats"] == "eightfold"],
)
def test_eightfold_returns_jobs(name, career_url):
    import requests
    parts = career_url.split("|")
    base_url = parts[0].rstrip("/")
    domain = parts[1] if len(parts) > 1 else ""
    location = parts[2] if len(parts) > 2 else "Israel"
    r = requests.get(
        f"{base_url}/api/pcsx/search",
        params={"domain": domain, "query": "", "location": location, "start": 0, "num": 20},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    positions = r.json().get("data", {}).get("positions", []) or []
    assert len(positions) >= 1, f"{name}: got 0 positions from Eightfold (career_url={career_url})"


# ---------------------------------------------------------------------------
# Gem.com job board API
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,slug",
    [(c["name"], c["slug"]) for c in ENABLED if c["ats"] == "gem"],
)
def test_gem_returns_jobs(name, slug):
    import requests
    r = requests.get(
        f"https://api.gem.com/job_board/v0/{slug}/job_posts/",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    jobs = r.json()
    assert isinstance(jobs, list), f"{name}: expected list from Gem API"
    assert len(jobs) >= 1, f"{name}: got 0 jobs from Gem (slug={slug})"


# ---------------------------------------------------------------------------
# Amazon Jobs (Israel)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,career_url",
    [(c["name"], c["career_url"]) for c in ENABLED if c["ats"] == "amazon"],
)
def test_amazon_returns_jobs(name, career_url):
    import requests
    r = requests.get(
        "https://www.amazon.jobs/en/search.json",
        params={"base_query": "", "loc_query": "Israel", "country": "ISR", "result_limit": 5, "page": 1},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=20,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    data = r.json()
    jobs = data.get("jobs", [])
    assert len(jobs) >= 1, f"{name}: got 0 jobs from Amazon API"


# ---------------------------------------------------------------------------
# Google Careers (Israel)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,career_url",
    [(c["name"], c["career_url"]) for c in ENABLED if c["ats"] == "google"],
)
def test_google_returns_jobs(name, career_url):
    import requests, re, json
    r = requests.get(
        "https://careers.google.com/jobs/results/",
        params={"location": "Israel", "q": "", "page": 1},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    match = re.search(r"AF_initDataCallback\(\{key:\s*'ds:1'[^,]*,\s*hash:\s*'\d+'[^,]*,\s*data:", r.text)
    assert match, f"{name}: ds:1 callback not found in Google Careers HTML"


# ---------------------------------------------------------------------------
# Apple Jobs (Israel)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,career_url",
    [(c["name"], c["career_url"]) for c in ENABLED if c["ats"] == "apple"],
)
def test_apple_returns_jobs(name, career_url):
    import requests, re, json
    r = requests.get(
        "https://jobs.apple.com/en-il/search",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    # Use same pattern as scraper: require ); or )\n to end the match robustly
    match = re.search(
        r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((.+?)\)(?:;|\n)',
        r.text,
        re.DOTALL,
    )
    assert match, f"{name}: hydration data not found in Apple Jobs HTML"
    outer = json.loads(match.group(1))
    data = json.loads(outer)
    total = data.get("loaderData", {}).get("search", {}).get("totalRecords", 0)
    assert total >= 1, f"{name}: totalRecords=0 from Apple Jobs"


# ---------------------------------------------------------------------------
# IBM Careers Israel (Elasticsearch API)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_ibm_returns_jobs():
    import requests
    r = requests.post(
        "https://www-api.ibm.com/search/api/v2",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://www.ibm.com/",
            "User-Agent": "Mozilla/5.0",
        },
        json={
            "appId": "careers",
            "scopes": ["careers2"],
            "query": {"bool": {"must": []}},
            "post_filter": {"term": {"field_keyword_05": "Israel"}},
            "size": 10,
            "sort": [{"_score": "desc"}],
            "lang": "zz",
            "localeSelector": {},
            "sm": {"query": "", "lang": "zz"},
        },
        timeout=20,
    )
    assert r.status_code == 200, f"IBM API: HTTP {r.status_code}"
    hits = r.json().get("hits", {}).get("hits", [])
    assert len(hits) >= 1, "IBM Israel: got 0 jobs from Elasticsearch API"


# ---------------------------------------------------------------------------
# PhenomPeople /widgets API (Thales, Cisco)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize("name,url,page_id,ref_num", [
    ("Thales Israel", "https://careers.thalesgroup.com", "page18", None),
    ("Cisco Israel",  "https://careers.cisco.com",       "page4",  "CISCISGLOBAL"),
])
def test_phenom_returns_jobs(name, url, page_id, ref_num):
    import requests
    payload = {
        "lang": "en_global", "deviceType": "desktop", "country": "global",
        "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "",
        "subsearch": "", "from": 0, "jobs": True, "counts": True,
        "all_fields": ["category", "country", "state", "city", "type"],
        "size": 5, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
        "pageId": page_id, "siteType": "external", "keywords": "", "global": True,
        "selected_fields": {"country": ["Israel"]}, "locationData": {},
    }
    if ref_num:
        payload["refNum"] = ref_num
    r = requests.post(f"{url}/widgets", json=payload, timeout=20)
    assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
    total = r.json().get("refineSearch", {}).get("totalHits", 0)
    assert total >= 1, f"{name}: totalHits=0 from /widgets"


# ---------------------------------------------------------------------------
# Check Point (Playwright + AWS WAF)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_checkpoint_returns_jobs():
    from playwright.sync_api import sync_playwright
    import re
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox",
            "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.goto("https://careers.checkpoint.com/", timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        page.goto(
            "https://careers.checkpoint.com/index.php?q=&module=cpcareers&a=search"
            "&fa%5B%5D=country_ss%3AIsrael&sort=&rows=500",
            timeout=25000, wait_until="domcontentloaded",
        )
        page.wait_for_timeout(2000)
        html = page.content()
        browser.close()
    m = re.search(r'<span id="resSize">(\d+)</span>', html)
    assert m, "Check Point: resSize not found — WAF may have blocked"
    count = int(m.group(1))
    assert count >= 1, f"Check Point: {count} Israel jobs found"


# ---------------------------------------------------------------------------
# Akamai Israel (Oracle HCM REST API)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_akamai_returns_jobs():
    import requests
    oracle_url = (
        "https://fa-extu-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitions"
        "?onlyData=true&expand=requisitionList.workLocation"
        "&finder=findReqs;siteNumber=CX_1,facetsList=LOCATIONS,limit=25,"
        "locationId=300000000469279,sortBy=POSTING_DATES_DESC"
    )
    r = requests.get(oracle_url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, timeout=15)
    assert r.status_code == 200, f"Akamai Oracle API: HTTP {r.status_code}"
    items = r.json().get("items", [])
    jobs = items[0].get("requisitionList", []) if items else []
    assert len(jobs) >= 1, "Akamai: 0 Israel jobs from Oracle HCM API"


# ---------------------------------------------------------------------------
# Radware (Taleo AJAX)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_radware_returns_jobs():
    import requests, re
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://radware.taleo.net/careersection/ex/joblist.ftl",
        "X-Requested-With": "XMLHttpRequest",
    })
    # Warm up session
    session.get("https://radware.taleo.net/careersection/ex/joblist.ftl", timeout=15)
    r = session.post(
        "https://radware.taleo.net/careersection/ex/joblist.ajax",
        data=(
            "ftlpageid=reqListAllJobsPage&ftlinterfaceid=requisitionListInterface"
            "&ftlcompid=validateTimeZoneId&jsfCmdId=validateTimeZoneId"
            "&ftlcompclass=InitTimeZoneAction&ftlcallback=requisition_restoreDatesValues"
            "&ftlajaxid=ftlx1&tz=GMT%252B00%253A00&tzname=UTC&lang=en"
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    assert r.status_code == 200, f"Radware Taleo AJAX: HTTP {r.status_code}"
    il_locs = re.findall(r"IL-IL-[^!]+", r.text)
    assert len(il_locs) >= 1, "Radware Taleo: no IL-IL-* locations found in AJAX response"


# ---------------------------------------------------------------------------
# Tower Semiconductor (Israel page HTML scraper)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_towersemi_returns_jobs():
    import requests, re
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
    })
    session.get("https://careers.towersemi.com/", timeout=15)  # warm Cloudflare session
    r = session.get("https://careers.towersemi.com/our-loactions/israel/", timeout=15)
    assert r.status_code == 200, f"Tower Semi Israel page: HTTP {r.status_code}"
    matches = re.findall(
        r'<a[^>]+href="(/job-description\?job_id=(\d+))"[^>]*>([^<]+)</a>',
        r.text, re.IGNORECASE
    )
    assert len(matches) >= 1, "Tower Semi: no job links found on Israel page"


# ---------------------------------------------------------------------------
# Elbit Systems (Niloo API)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_elbit_returns_jobs():
    import requests
    r = requests.post(
        "https://niloo-server.herokuapp.com/actions-elbit",
        json={"cmd": "get-jobs"},
        headers={"Content-Type": "application/json",
                 "Referer": "https://elbitsystemscareer.com/"},
        timeout=40,
    )
    assert r.status_code in (200, 201), f"Elbit Niloo API: HTTP {r.status_code}"
    jobs = r.json()
    assert isinstance(jobs, list), "Elbit: expected list response"
    assert len(jobs) >= 1, "Elbit: got 0 jobs from Niloo API"


# ---------------------------------------------------------------------------
# End-to-end: each enabled company's scraper returns ≥ 1 RawJob
# ---------------------------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize(
    "name,ats,slug,career_url",
    COMPANY_IDS,
)
def test_scraper_integration(name, ats, slug, career_url):
    """Full integration: scraper class → ≥1 RawJob returned."""
    from models.job import CompanyConfig

    company = CompanyConfig(
        name=name,
        ats=ats,
        slug=slug,
        career_url=career_url,
        enabled=True,
    )

    if ats == "greenhouse":
        from scrapers.greenhouse import GreenhouseScraper
        scraper = GreenhouseScraper(request_delay=0)
    elif ats == "lever":
        from scrapers.lever import LeverScraper
        scraper = LeverScraper(request_delay=0)
    elif ats == "workday":
        from scrapers.workday import WorkdayScraper
        scraper = WorkdayScraper(request_delay=0)
    elif ats == "comeet":
        from scrapers.comeet import ComeetScraper
        scraper = ComeetScraper(request_delay=0)
    elif ats == "mobileye":
        from scrapers.mobileye import MobileyeScraper
        scraper = MobileyeScraper(request_delay=0)
    elif ats == "varonis":
        from scrapers.varonis import VaronisScraper
        scraper = VaronisScraper(request_delay=0)
    elif ats == "smartrecruiters":
        from scrapers.smartrecruiters import SmartRecruitersScraper
        scraper = SmartRecruitersScraper(request_delay=0)
    elif ats == "eightfold":
        from scrapers.eightfold import EightfoldScraper
        scraper = EightfoldScraper(request_delay=0)
    elif ats == "gem":
        from scrapers.gem import GemScraper
        scraper = GemScraper(request_delay=0)
    elif ats == "amazon":
        from scrapers.amazon import AmazonScraper
        scraper = AmazonScraper(request_delay=0)
    elif ats == "google":
        from scrapers.google import GoogleScraper
        scraper = GoogleScraper(request_delay=0)
    elif ats == "apple":
        from scrapers.apple import AppleScraper
        scraper = AppleScraper(request_delay=0)
    elif ats == "ibm":
        from scrapers.ibm import IBMScraper
        scraper = IBMScraper(request_delay=0)
    elif ats == "elbit":
        from scrapers.elbit import ElbitScraper
        scraper = ElbitScraper(request_delay=0)
    elif ats == "phenom":
        from scrapers.phenom import PhenomScraper
        scraper = PhenomScraper(request_delay=0)
    elif ats == "checkpoint":
        from scrapers.checkpoint import CheckPointScraper
        scraper = CheckPointScraper(request_delay=0)
    elif ats == "towersemi":
        from scrapers.towersemi import TowerSemiScraper
        scraper = TowerSemiScraper(request_delay=0)
    elif ats == "akamai":
        from scrapers.akamai import AkamaiScraper
        scraper = AkamaiScraper(request_delay=0)
    elif ats == "radware":
        from scrapers.radware import RadwareScraper
        scraper = RadwareScraper(request_delay=0)
    else:
        pytest.skip(f"No scraper for ATS '{ats}'")

    jobs = scraper.fetch_jobs(company, max_age_days=365)
    assert len(jobs) >= 1, (
        f"{name} ({ats}): scraper returned 0 jobs — "
        f"slug={slug!r}, career_url={career_url!r}"
    )
    job = jobs[0]
    assert job.title, f"{name}: first job has no title"
    assert job.url, f"{name}: first job has no URL"
    assert job.company == name, f"{name}: company mismatch"
