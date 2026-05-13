"""
Allot careers scraper.

Allot hosts job listings on a custom WordPress site (allot.com/careers/search/).
Job data is served via a WordPress AJAX endpoint that returns HTML.

API:
  POST https://www.allot.com/wp-admin/admin-ajax.php
  Form data: action=get_positions, cr=<region>, cd=<dept>, cs=<search>
  Response: HTML fragment with job listing cards

Each job card contains: region, department, location, title, vacancy URL.
The vacancy URL (/careers/vacancy/{uid}/) is the canonical job page.

Standalone usage:
    python -m scrapers.allot --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.allot")

_CAREERS_URL = "https://www.allot.com/careers/search/"
_AJAX_URL = "https://www.allot.com/wp-admin/admin-ajax.php"
_JOB_PATTERN = re.compile(
    r'<a class="offer" href="(/careers/vacancy/([^/]+)/)">\s*'
    r'<div class="ring[^"]*">\s*<span>([^<]+)</span>.*?'
    r'<div class="vacancy">([^<]+)</div>\s*'
    r'<div class="location">(.*?)</div>\s*'
    r'<div class="title">([^<]+)</div>',
    re.DOTALL,
)


def _clean_location(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw).strip()
    text = re.sub(r"\s+", " ", text).strip(",").strip()
    return text


class AllotScraper(BaseScraper):
    """Scrapes Allot job listings via WordPress AJAX endpoint."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": _CAREERS_URL,
            "Origin": "https://www.allot.com",
            "X-Requested-With": "XMLHttpRequest",
        })

    def _init_session(self) -> None:
        """Fetch careers page to pick up cookies before calling AJAX."""
        self._session.get(_CAREERS_URL, timeout=self._timeout)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_listings(self) -> str:
        resp = self._session.post(
            _AJAX_URL,
            data={"action": "get_positions"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.text

    def _fetch_description(self, vacancy_url: str) -> str:
        """Fetch job description from the Allot vacancy page."""
        try:
            resp = self._session.get(vacancy_url, timeout=self._timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            desc = soup.select_one("div.job-content")
            if desc:
                return desc.decode_contents().strip()
        except Exception as exc:
            _log.error(f"Could not fetch description from {vacancy_url}: {exc}", exc_info=True)
        return ""

    def _normalize(self, url_path: str, uid: str, region: str, dept: str,
                   location_raw: str, title: str, company: CompanyConfig) -> Optional[RawJob]:
        title = title.strip()
        if not title:
            return None
        location = _clean_location(location_raw)
        return RawJob(
            id=f"allot:{uid}",
            title=title,
            company=company.name,
            url=f"https://www.allot.com{url_path}",
            location_raw=location,
            posted_date=None,
            description_html="",
            source="allot",
            raw_payload={
                "uid": uid, "region": region.strip(),
                "dept": dept.strip(), "location": location,
            },
        )

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Allot job listings filtered to Israel."""
        try:
            self._init_session()
            html = self._fetch_listings()
        except requests.HTTPError as e:
            _log.error(f"HTTP error: {e}", exc_info=True)
            return []
        except Exception as e:
            _log.error(f"Error: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for m in _JOB_PATTERN.finditer(html):
            url_path, uid, region, dept, location_raw, title = m.groups()
            location = _clean_location(location_raw)
            # Filter to Israel locations
            if not any(x in location.lower() for x in ["israel", "il,", "tel aviv", "herzliya",
                                                         "petah", "haifa", "netanya", "be'er"]):
                continue
            job = self._normalize(url_path, uid, region, dept, location_raw, title, company)
            if job:
                time.sleep(self._delay)
                description_html = self._fetch_description(job.url)
                if description_html:
                    job = job.model_copy(update={"description_html": description_html})
                jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Allot careers")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--all", action="store_true", help="Return all jobs, not just Israel")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name="Allot",
        ats="allot",
        slug=None,
        career_url=_CAREERS_URL,
        enabled=True,
    )

    scraper = AllotScraper()

    if args.all:
        # Return all jobs for debugging
        scraper._init_session()
        html = scraper._fetch_listings()
        jobs_raw = []
        for m in _JOB_PATTERN.finditer(html):
            url_path, uid, region, dept, location_raw, title = m.groups()
            jobs_raw.append({
                "title": title.strip(),
                "location": _clean_location(location_raw),
                "region": region.strip(),
                "dept": dept.strip(),
                "url": f"https://www.allot.com{url_path}",
            })
        print(f"All jobs ({len(jobs_raw)}):")
        for j in jobs_raw:
            print(f"  {j['title']} @ {j['location']} [{j['region']}]")
        return

    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} Israel jobs", file=sys.stderr)
        for j in jobs:
            print(f"  {j.title} @ {j.location_raw}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
