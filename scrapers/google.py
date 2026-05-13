"""
Google Careers scraper (Israel).

Google Careers embeds all job data as server-rendered JSON inside the page HTML
via AF_initDataCallback. No public REST API exists.

Strategy:
  1. Fetch https://careers.google.com/jobs/results/?location=Israel&q=&page={N}
  2. Extract the ds:1 AF_initDataCallback block using bracket counting
  3. Parse the nested array structure

Job array fields (by index):
  [0]  = job ID (numeric string)
  [1]  = title
  [2]  = signin redirect URL — NOT the viewable job page; skip this
  [3]  = [None, responsibilities HTML]
  [4]  = [None, minimum qualifications HTML]
  [7]  = company name
  [9]  = list of location tuples [display_name, [address], city, None, district, country_code]
  [10] = [None, job description intro HTML]
  [12] = [unix_timestamp_seconds, nanoseconds] of post date
  [19] = [None, preferred qualifications HTML]

Job URL: https://careers.google.com/jobs/results/{job_id}-{title-slug}/

Standalone usage:
    python -m scrapers.google --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
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
from scrapers.base import BaseScraper, build_search_query

_log = logging.getLogger("job_searcher.scrapers.google")

_SEARCH_URL = "https://careers.google.com/jobs/results/"
_JOB_BASE_URL = "https://careers.google.com/jobs/results"
_PAGE_SIZE = 20


def _extract_balanced(text: str, start: int) -> Optional[str]:
    """Extract the balanced JSON array/object starting at text[start]."""
    open_char = text[start]
    close_char = "]" if open_char == "[" else "}"
    depth = 0
    in_str = False
    escape = False

    for i, c in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _build_job_url(job_id: str, title: str) -> str:
    """Build canonical Google Careers job page URL from job ID and title."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{_JOB_BASE_URL}/{job_id}-{slug}/"


def _extract_html_field(field) -> str:
    """Extract HTML string from a [None, html_string] field, or empty string."""
    if isinstance(field, list) and len(field) >= 2 and isinstance(field[1], str):
        return field[1]
    if isinstance(field, str):
        return field
    return ""


class GoogleScraper(BaseScraper):
    """Scrapes Google Careers Israel listings from embedded HTML data."""

    def __init__(self, request_delay: float = 2.0, timeout: int = 30,
                 job_titles: list[str] = []):
        self._delay = request_delay
        self._timeout = timeout
        self._query = build_search_query(job_titles)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=20),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_page(self, page: int) -> tuple[list, Optional[int]]:
        """Fetch one page. Returns (jobs_list, total_count)."""
        resp = self._session.get(
            _SEARCH_URL,
            params={"location": "Israel", "q": self._query, "page": page},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        html = resp.text

        match = re.search(
            r"AF_initDataCallback\(\{key:\s*'ds:1'[^,]*,\s*hash:\s*'\d+'[^,]*,\s*data:",
            html,
        )
        if not match:
            return [], None

        data_str = _extract_balanced(html, match.end())
        if not data_str:
            return [], None

        data = json.loads(data_str)
        jobs_list = data[0] if data else []
        total = data[2] if len(data) > 2 and isinstance(data[2], int) else None
        return jobs_list, total

    def _normalize(self, raw: list, company: CompanyConfig) -> Optional[RawJob]:
        try:
            job_id = str(raw[0])
            title = raw[1] if len(raw) > 1 else ""
            if not title:
                return None

            # Canonical job page URL — raw[2] is a sign-in redirect, not the viewable page
            url = _build_job_url(job_id, title)

            # Location: raw[9] = list of [display_name, [address], city, None, district, country]
            locations = raw[9] if len(raw) > 9 else []
            if locations:
                loc_parts = [loc[0] for loc in locations if loc and loc[0]]
                location_raw = "; ".join(loc_parts) if loc_parts else None
            else:
                location_raw = None

            # Timestamp: raw[12] = [unix_seconds, nanoseconds]
            posted_date = None
            ts_entry = raw[12] if len(raw) > 12 else None
            if ts_entry and isinstance(ts_entry, list) and ts_entry[0]:
                try:
                    posted_date = datetime.fromtimestamp(ts_entry[0], tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            # Description: combine intro (raw[10]), responsibilities (raw[3]),
            # min qualifications (raw[4]), preferred qualifications (raw[19])
            parts = []
            for idx in [10, 3, 4, 19]:
                if len(raw) > idx:
                    html_frag = _extract_html_field(raw[idx])
                    if html_frag:
                        parts.append(html_frag)
            description_html = "\n".join(parts)

            return RawJob(
                id=f"google:{job_id}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=posted_date,
                description_html=description_html,
                source="google",
                raw_payload={"id": job_id, "title": title},
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from Google Careers."""
        cutoff = datetime.now(tz=timezone.utc).timestamp() - max_age_days * 86400

        all_jobs: list[RawJob] = []
        page = 1
        total = None

        while True:
            try:
                raw_jobs, page_total = self._fetch_page(page)
            except requests.HTTPError as e:
                _log.error(f"HTTP error page {page}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error page {page}: {e}", exc_info=True)
                break

            if total is None and page_total is not None:
                total = page_total

            if not raw_jobs:
                break

            stop_early = False
            for raw in raw_jobs:
                job = self._normalize(raw, company)
                if not job:
                    continue
                if job.posted_date and job.posted_date.timestamp() < cutoff:
                    stop_early = True
                    continue
                all_jobs.append(job)

            if stop_early:
                break

            if total is not None and page * _PAGE_SIZE >= total:
                break

            page += 1
            time.sleep(self._delay)

        time.sleep(self._delay)
        return all_jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Google Careers Israel")
    parser.add_argument("--company", default="Google Israel", help="Company name")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="google",
        slug=None,
        career_url="https://careers.google.com/jobs/results/?location=Israel",
        enabled=True,
    )

    scraper = GoogleScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {args.company}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw} | {job.url}", file=sys.stderr)
            if job.description_html:
                print(f"    desc: {job.description_html[:100]}...", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
