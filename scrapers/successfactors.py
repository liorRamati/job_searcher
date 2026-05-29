"""
SAP SuccessFactors Career Portal scraper (HTML-based).

SuccessFactors Career Portal (Job2Web) serves an HTML page with job listings
that can be parsed directly. The search URL uses query params:
  GET https://{company-jobs-domain}/search/restapi?searchby=location&q={location}

Response: HTML page with job rows inside a <tbody>, each row containing:
  <a class="jobTitle-link" href="/job/{slug}/{id}/"> - job title and URL
  <span class="jobLocation"> - location text
  Pagination: <span class="paginationLabel">Results N–M of T</span>
  Page param: &start={offset} (20 per page by default)

Standalone usage:
    python -m scrapers.successfactors --career-url "https://jobs.netapp.com/search/restapi?searchby=location&q=Israel" --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.successfactors")

_PAGE_SIZE = 20


class SuccessFactorsScraper(BaseScraper):
    """Scrapes SAP SuccessFactors Career Portal job listings via HTML parsing."""

    def __init__(self, request_delay: float = 1.5, timeout: int = 60,
                 job_titles: list[str] = []):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.HTTPError, requests.exceptions.ReadTimeout)),
        reraise=True,
    )
    def _fetch_page(self, url: str, start: int = 0) -> requests.Response:
        params = {"start": start} if start > 0 else {}
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp

    def _parse_jobs(self, html: str, base_url: str) -> tuple[list[dict], int]:
        """Parse job rows from SuccessFactors HTML.

        Returns (jobs, total) where each job dict has: title, url, location.
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        tbody = soup.find("tbody")
        if tbody:
            for row in tbody.find_all("tr"):
                link = row.find("a", class_="jobTitle-link")
                if not link:
                    continue
                title = link.get_text(strip=True)
                href = link.get("href", "")
                url = urljoin(base_url, href) if href else ""

                loc_el = row.find("span", class_="jobLocation")
                location = loc_el.get_text(strip=True) if loc_el else None

                if title and url:
                    jobs.append({"title": title, "url": url, "location": location})

        # Parse total from "Results N – M of T"
        total = len(jobs)
        label = soup.find("span", class_="paginationLabel")
        if label:
            m = re.search(r"of\s+<b>(\d+)</b>", str(label))
            if m:
                total = int(m.group(1))

        return jobs, total

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            url = raw["url"]
            # Extract job ID from URL path like /job/Petach-Tikva-.../1386913900/
            m = re.search(r"/(\d{7,})/?$", url)
            job_id = m.group(1) if m else url.split("/")[-2] if url.endswith("/") else url.split("/")[-1]

            return RawJob(
                id=f"successfactors:{job_id}",
                title=raw["title"],
                company=company.name,
                url=url,
                location_raw=raw.get("location"),
                posted_date=None,
                description_html="",
                source="successfactors",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from SuccessFactors Career Portal."""
        if not company.career_url:
            raise ValueError(f"SuccessFactors scraper requires a career_url for: {company.name}")

        parsed = urlparse(company.career_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        all_raw: list[dict] = []
        start = 0
        total = _PAGE_SIZE  # initial guess; updated on first response

        while start < total:
            try:
                resp = self._fetch_page(company.career_url, start=start)
            except requests.HTTPError as e:
                _log.error(f"HTTP error for {company.name}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error for {company.name}: {e}", exc_info=True)
                break

            page_jobs, total = self._parse_jobs(resp.text, base_url)
            if not page_jobs:
                break

            all_raw.extend(page_jobs)
            _log.debug(f"start={start}: got {len(page_jobs)} jobs, total={total}")

            start += len(page_jobs)
            if len(page_jobs) < _PAGE_SIZE:
                break
            time.sleep(self._delay)

        jobs = [j for raw in all_raw if (j := self._normalize(raw, company)) is not None]
        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scrape SAP SuccessFactors Career Portal jobs",
        epilog=__doc__,
    )
    parser.add_argument("--career-url", required=True, help="SuccessFactors search URL")
    parser.add_argument("--company", default="NetApp Israel")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Write JSON to this file (default: stdout)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="successfactors",
        slug=None,
        career_url=args.career_url,
        enabled=True,
    )

    scraper = SuccessFactorsScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {company.name}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}", file=sys.stderr)

    output = [j.model_dump() for j in jobs]
    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
