"""
Lever public job board scraper.

API endpoint:
  GET https://api.lever.co/v0/postings/{slug}?mode=json
  — No authentication required.
  — Returns all active job postings in a single JSON response.
  — Add `?mode=json` to get structured data with descriptionPlain.

Standalone usage:
    python -m scrapers.lever --company-id fiverr --max-age-days 30 --verbose
    python -m scrapers.lever --company-id outbrain --output /tmp/outbrain_jobs.json
"""

from __future__ import annotations

import argparse
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

_log = logging.getLogger("job_searcher.scrapers.lever")

_API_BASE = "https://api.lever.co/v0/postings"


class LeverScraper(BaseScraper):
    """Scrapes all active jobs from a Lever-hosted job board."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        """
        Parameters
        ----------
        request_delay : seconds to sleep after each company's API call.
        timeout       : HTTP request timeout in seconds.
        """
        self._delay   = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "JobSearchAgent/1.0"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_raw(self, slug: str) -> list[dict]:
        """Fetch the raw JSON response from the Lever API."""
        url = f"{_API_BASE}/{slug}?mode=json"
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        """Convert one raw Lever job dict into a RawJob model."""
        try:
            job_id = raw.get("id", "")
            title = raw.get("text", "")
            location = raw.get("categories", {}).get("location")
            team = raw.get("categories", {}).get("team")
            description_plain = raw.get("descriptionPlain", "")
            hosted_url = raw.get("hostedUrl", "")
            created_at = raw.get("createdAt")

            if not title:
                return None

            posted_date = None
            if created_at:
                try:
                    posted_date = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            description_html = f"<p>{description_plain}</p>" if description_plain else ""

            if team and not title.startswith(team):
                title = f"{team} - {title}"

            return RawJob(
                id=f"lever:{job_id}",
                title=title,
                company=company.name,
                url=hosted_url,
                location_raw=location,
                posted_date=posted_date,
                description_html=description_html,
                source="lever",
                raw_payload=raw,
            )
        except Exception as e:
            _log.warning(f"Failed to normalize Lever job: {e}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch and normalize job listings from Lever."""
        if not company.slug:
            raise ValueError(f"Lever scraper requires a slug for company: {company.name}")

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        try:
            raw_jobs = self._fetch_raw(company.slug)
        except requests.HTTPError as e:
            _log.error(f"Error fetching Lever jobs for {company.name}: {e}", exc_info=True)
            return []

        jobs = []
        for raw in raw_jobs:
            job = self._normalize(raw, company)
            if job and job.posted_date and job.posted_date >= cutoff:
                jobs.append(job)
            elif job and not job.posted_date:
                jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Lever job board")
    parser.add_argument("--company-id", required=True, help="Lever company slug")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Output file path (JSON)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company_id,
        ats="lever",
        slug=args.company_id,
        enabled=True,
    )

    scraper = LeverScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {company.name}")
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}")

    if args.output:
        import json
        with open(args.output, "w") as f:
            json.dump([job.model_dump() for job in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}")

    if not args.dry_run and not args.output:
        print(f"Found {len(jobs)} jobs (use --output to save)")


if __name__ == "__main__":
    main()