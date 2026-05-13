"""
Comeet public job board scraper.

Comeet is an Israeli ATS popular among Israeli tech companies.

API endpoint (list all positions):
  GET https://www.comeet.co/careers-api/2.0/company/{company_uid}/positions?token={token}
  — No authentication required for public job boards.
  — Returns all active positions in a single JSON response.

Individual position:
  GET https://www.comeet.co/careers-api/2.0/company/{company_uid}/positions/{position_uid}?token={token}

Finding the company_uid and token:
  1. View the company's career page source
  2. Search for `COMEET.init({` — the `company-uid` and `token` fields are plaintext

Standalone usage:
    python -m scrapers.comeet --company-uid 41.009 --token 14952452466D3DB7B61495240B91 --company-name eToro --verbose
    python -m scrapers.comeet --company-uid 41.00B --token 14B52C52C67790D3E1296BA37C20 --company-name Monday.com --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.comeet")

_API_BASE = "https://www.comeet.co/careers-api/2.0/company"


class ComeetScraper(BaseScraper):
    """Scrapes all active jobs from a Comeet-hosted job board."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "JobSearchAgent/1.0"})

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_all(self, company_uid: str, token: str) -> list[dict]:
        """Fetch all positions from the Comeet API."""
        url = f"{_API_BASE}/{company_uid}/positions"
        resp = self._session.get(url, params={"token": token}, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        return data

    def _fetch_description_playwright(self, hosted_url: str) -> str:
        """Use Playwright to render the AngularJS Comeet job page and extract description.

        The Comeet hosted page loads content via AngularJS, so plain HTTP gives
        an empty template. After rendering, description is in div.userDesignedContent.
        """
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                ))
                page.goto(hosted_url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)

                divs = page.query_selector_all("div.userDesignedContent")
                parts = []
                for div in divs:
                    html = page.evaluate("el => el.innerHTML", div)
                    if html and html.strip():
                        parts.append(html)

                browser.close()
                return "\n".join(parts)
        except Exception as exc:
            _log.error(f"Playwright error for {hosted_url}: {exc}", exc_info=True)
            return ""

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        """Convert one Comeet position dict into a RawJob."""
        try:
            name = raw.get("name", "").strip()
            if not name:
                return None

            uid = raw.get("uid", "")
            # Prefer the URL on the company's own career page
            job_url = raw.get("url_active_page") or raw.get("url_comeet_hosted_page") or ""

            # Location: Comeet returns a full dict
            loc_data = raw.get("location") or {}
            if isinstance(loc_data, dict):
                city = loc_data.get("city") or loc_data.get("name") or ""
                country = loc_data.get("country") or ""
                location_raw = f"{city}, {country}".strip(", ") or None
            else:
                location_raw = str(loc_data) if loc_data else None

            # Department / team for enriched title if needed
            department = raw.get("department") or ""

            # time_updated is a Unix timestamp in milliseconds
            time_updated = raw.get("time_updated")
            posted_date: Optional[datetime] = None
            if time_updated:
                try:
                    posted_date = datetime.fromtimestamp(int(time_updated) / 1000, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            hosted_url = raw.get("url_comeet_hosted_page") or ""
            return RawJob(
                id=f"comeet:{uid}",
                title=name,
                company=company.name,
                url=job_url,
                location_raw=location_raw,
                posted_date=posted_date,
                description_html="",  # populated in fetch_jobs
                source="comeet",
                raw_payload={**raw, "_hosted_url": hosted_url},
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch and normalize job listings from Comeet."""
        if not company.slug:
            raise ValueError(f"Comeet scraper requires slug='company_uid:token' for: {company.name}")

        # slug format: "company_uid:token"
        if ":" not in company.slug:
            raise ValueError(
                f"Comeet slug must be 'company_uid:token', got: {company.slug!r}"
            )
        company_uid, token = company.slug.split(":", 1)

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        try:
            raw_jobs = self._fetch_all(company_uid, token)
        except requests.HTTPError as e:
            _log.error(f"HTTP error for {company.name}: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for raw in raw_jobs:
            job = self._normalize(raw, company)
            if job is None:
                continue
            if job.posted_date and job.posted_date < cutoff:
                continue
            # Fetch description via Playwright (AngularJS-rendered page)
            hosted_url = raw.get("url_comeet_hosted_page") or ""
            if hosted_url:
                time.sleep(self._delay)
                description_html = self._fetch_description_playwright(hosted_url)
                if description_html:
                    job = job.model_copy(update={"description_html": description_html})
            jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Comeet job board")
    parser.add_argument("--company-uid", required=True, help="Comeet company UID (e.g. 41.009)")
    parser.add_argument("--token", required=True, help="Comeet API token")
    parser.add_argument("--company-name", default=None)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Output file path (JSON)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company_name or args.company_uid,
        ats="comeet",
        slug=f"{args.company_uid}:{args.token}",
        enabled=True,
    )

    scraper = ComeetScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {company.name}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}", file=sys.stderr)

    if args.output:
        import json
        with open(args.output, "w") as f:
            json.dump([job.model_dump() for job in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        import json
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
