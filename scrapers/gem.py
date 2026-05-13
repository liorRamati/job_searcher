"""
Gem.com job board scraper.

Some companies host their career page via Gem.com (talent management platform).
The public API endpoint is:
  GET https://api.gem.com/job_board/v0/{company_slug}/job_posts/

Response: list of job objects with title, location, absolute_url, content, etc.

Known companies using this:
  - BigPanda → slug "bigpanda"

Standalone usage:
    python -m scrapers.gem --verbose
"""

from __future__ import annotations

import argparse
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
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.gem")

_API_BASE = "https://api.gem.com/job_board/v0"


class GemScraper(BaseScraper):
    """Scrapes Gem.com-hosted job boards."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
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
    def _fetch_raw(self, slug: str) -> list[dict]:
        resp = self._session.get(
            f"{_API_BASE}/{slug}/job_posts/",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            title = raw.get("title", "").strip()
            if not title:
                return None

            loc_data = raw.get("location")
            if isinstance(loc_data, dict):
                location_raw = loc_data.get("name") or None
            elif isinstance(loc_data, str):
                location_raw = loc_data or None
            else:
                location_raw = None

            url = raw.get("absolute_url", "")
            if not url:
                return None

            job_id = url.rstrip("/").split("/")[-1]

            return RawJob(
                id=f"gem:{company.slug}:{job_id}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=None,
                description_html=raw.get("content", ""),
                source="gem",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch all jobs from a Gem.com job board."""
        slug = company.slug
        if not slug:
            _log.warning(f"No slug for {company.name}")
            return []

        try:
            raw_jobs = self._fetch_raw(slug)
        except requests.HTTPError as e:
            _log.error(f"HTTP error for {company.name}: {e}", exc_info=True)
            return []
        except Exception as e:
            _log.error(f"Error for {company.name}: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for raw in raw_jobs:
            job = self._normalize(raw, company)
            if job:
                jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape a Gem.com job board")
    parser.add_argument("--slug", required=True, help="Gem.com company slug (e.g. bigpanda)")
    parser.add_argument("--company", default="Company", help="Company name")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="gem",
        slug=args.slug,
        career_url=None,
        enabled=True,
    )

    scraper = GemScraper()
    jobs = scraper.fetch_jobs(company)

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
