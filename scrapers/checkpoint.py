"""
Check Point Software careers scraper.

Check Point hosts jobs on careers.checkpoint.com — a custom PHP/Solr system
protected by AWS WAF. Direct HTTP requests receive a 202 challenge page.
Playwright is required to solve the JavaScript challenge and retrieve the HTML.

Flow:
  1. Visit homepage to trigger WAF cookie issuance
  2. Fetch all Israel jobs with rows=500 in a single request
  3. Parse each <div class="position"> card from the HTML

Job card fields extracted:
  data-id / joborderid  → numeric job ID
  data-title            → job title
  .professions div      → profession/category
  .posInfo p.place      → location string
  .posInfo second p     → department | type | job ID
  div.resp.shortResp    → short job description HTML

Job URL: https://careers.checkpoint.com/index.php?m=cpcareers&a=show&joborderid={id}

Standalone usage:
    python -m scrapers.checkpoint --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.checkpoint")

_SEARCH_URL = (
    "https://careers.checkpoint.com/index.php"
    "?q=&module=cpcareers&a=search&fa%5B%5D=country_ss%3AIsrael&sort=&rows=500"
)
_JOB_URL_TMPL = (
    "https://careers.checkpoint.com/index.php"
    "?m=cpcareers&a=show&joborderid={job_id}"
)


def _inner_text(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _parse_position_block(block: str) -> Optional[dict]:
    """Extract fields from one <div class="position"> HTML block."""
    soup = BeautifulSoup(block, "html.parser")

    # Job ID and title from data-id / data-title on the save button
    save_btn = soup.find(attrs={"data-id": True, "data-title": True})
    if save_btn:
        job_id = save_btn["data-id"]
        title = save_btn["data-title"].strip()
    else:
        # Fallback: extract from anchor href
        anchor = soup.find("a", href=re.compile(r"joborderid=(\d+)"))
        if not anchor:
            return None
        m = re.search(r"joborderid=(\d+)", anchor["href"])
        if not m:
            return None
        job_id = m.group(1)
        title = anchor.get_text(strip=True)

    # Profession/category
    prof_div = soup.find("div", class_="professions")
    profession = prof_div.get_text(strip=True) if prof_div else ""

    # Location
    place_p = soup.find("p", class_="place")
    location = place_p.get_text(strip=True) if place_p else ""

    # Department (second <p> inside .posInfo)
    posinfo = soup.find("div", class_="posInfo")
    department = ""
    if posinfo:
        ps = posinfo.find_all("p")
        if len(ps) >= 2:
            info_text = ps[1].get_text(strip=True)
            # "R&D | Full Time | Job Id: 25511"
            parts = [p.strip() for p in info_text.split("|")]
            department = parts[0] if parts else ""

    # Short description — use CSS selector to avoid fragile closing-tag regex
    resp_div = soup.select_one("div.resp.shortResp")
    description_html = resp_div.decode_contents().strip() if resp_div else ""

    return {
        "job_id": job_id,
        "title": title,
        "profession": profession,
        "location": location,
        "department": department,
        "description_html": description_html,
    }


class CheckPointScraper(BaseScraper):
    """Scrapes Check Point careers via Playwright (AWS WAF bypass)."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout

    def _fetch_html(self) -> str:
        """Use Playwright to load the Israel jobs page and return its HTML."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()

            # Warm up WAF cookie
            page.goto("https://careers.checkpoint.com/", timeout=self._timeout * 1000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(1000)

            # Fetch all Israel jobs
            page.goto(_SEARCH_URL, timeout=self._timeout * 1000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            html = page.content()
            browser.close()
            return html

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Check Point Israel jobs via Playwright HTML scraping."""
        try:
            html = self._fetch_html()
        except Exception as e:
            _log.error(f"Error loading page: {e}", exc_info=True)
            return []

        # Verify we got real job results
        count_match = re.search(r'<span id="resSize">(\d+)</span>', html)
        if not count_match:
            _log.warning("Could not find job count — WAF may have blocked")
            return []

        # Split into per-job blocks
        pos_start = html.find('id="positionResults"')
        if pos_start < 0:
            _log.warning("positionResults section not found")
            return []

        positions_html = html[pos_start:]
        starts = [m.start() for m in re.finditer(r'<div class="position">', positions_html)]

        jobs: list[RawJob] = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(positions_html)
            block = positions_html[start:end]
            parsed = _parse_position_block(block)
            if not parsed or not parsed["title"]:
                continue

            jobs.append(RawJob(
                id=f"checkpoint:{parsed['job_id']}",
                title=parsed["title"],
                company=company.name,
                url=_JOB_URL_TMPL.format(job_id=parsed["job_id"]),
                location_raw=parsed["location"] or "Israel",
                posted_date=None,
                description_html=parsed["description_html"],
                source="checkpoint",
                raw_payload={
                    "job_id": parsed["job_id"],
                    "profession": parsed["profession"],
                    "department": parsed["department"],
                },
            ))

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Check Point careers")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name="Check Point", ats="checkpoint", slug=None,
        career_url="https://careers.checkpoint.com", enabled=True,
    )
    scraper = CheckPointScraper()
    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} jobs", file=sys.stderr)
        for j in jobs:
            print(f"  {j.title} @ {j.location_raw}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
