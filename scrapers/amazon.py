"""
Amazon Jobs scraper.

Uses the public search.json API with country and location filters:
  GET https://www.amazon.jobs/en/search.json?loc_query=Israel&country=ISR&result_limit=10&sort=recent&page={N}

Response: {"hits": <total_count>, "jobs": [{title, normalized_location, job_path, posted_date, ...}]}
Job URL: https://www.amazon.jobs + job_path

Standalone usage:
    python -m scrapers.amazon --verbose
"""

from __future__ import annotations

import argparse
import math
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

_log = logging.getLogger("job_searcher.scrapers.amazon")

_API_BASE = "https://www.amazon.jobs/en/search.json"
_BASE_URL = "https://www.amazon.jobs"
_PAGE_SIZE = 10


class AmazonScraper(BaseScraper):
    """Scrapes Amazon Jobs Israel listings via the public search JSON API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30,
                 job_titles: list[str] = []):
        self._delay = request_delay
        self._timeout = timeout
        self._query = build_search_query(job_titles)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_page(self, page: int) -> tuple[int, list[dict]]:
        resp = self._session.get(
            _API_BASE,
            params={
                "base_query": self._query,
                "loc_query": "Israel",
                "country": "ISR",
                "result_limit": _PAGE_SIZE,
                "sort": "recent",
                "page": page,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("hits", 0), data.get("jobs", [])

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            title = raw.get("title", "").strip()
            if not title:
                return None

            job_path = raw.get("job_path", "")
            if not job_path:
                return None
            url = f"{_BASE_URL}{job_path}"

            location_raw = raw.get("normalized_location") or raw.get("location") or None

            # posted_date is a string like "April 30, 2026"
            posted_date = None
            date_str = raw.get("posted_date", "")
            if date_str:
                try:
                    posted_date = datetime.strptime(date_str, "%B %d, %Y").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            job_id = raw.get("id_icims") or raw.get("id") or job_path.rstrip("/").split("/")[-1]

            return RawJob(
                id=f"amazon:{job_id}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=posted_date,
                description_html=raw.get("description", ""),
                source="amazon",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from Amazon Jobs."""
        cutoff = datetime.now(tz=timezone.utc).timestamp() - max_age_days * 86400

        all_jobs: list[RawJob] = []
        page = 1
        total = None

        while True:
            try:
                total, raw_jobs = self._fetch_page(page)
            except requests.HTTPError as e:
                _log.error(f"HTTP error page {page}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error page {page}: {e}", exc_info=True)
                break

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
    parser = argparse.ArgumentParser(description="Scrape Amazon Jobs Israel")
    parser.add_argument("--company", default="Amazon", help="Company name")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="amazon",
        slug=None,
        career_url="https://www.amazon.jobs/en/locations/israel",
        enabled=True,
    )

    scraper = AmazonScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {args.company}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}", file=sys.stderr)

    if args.output:
        import json
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        import json
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
