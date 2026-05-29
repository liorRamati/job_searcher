"""
SmartRecruiters public job board scraper.

API endpoint:
  GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
  — No authentication required for public job listings.
  — Returns paginated results, need to handle pagination.

Standalone usage:
    python -m scrapers.smartrecruiters --company-id Amdocs --max-age-days 30 --verbose
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

_log = logging.getLogger("job_searcher.scrapers.smartrecruiters")

_API_BASE = "https://api.smartrecruiters.com/v1/companies"


class SmartRecruitersScraper(BaseScraper):
    """Scrapes all active jobs from a SmartRecruiters-hosted job board."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
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
    def _fetch_page(self, slug: str, offset: int = 0, limit: int = 100,
                    country: Optional[str] = None) -> dict:
        """Fetch one page of results from SmartRecruiters API."""
        url = f"{_API_BASE}/{slug}/postings"
        params: dict = {"offset": offset, "limit": limit}
        if country:
            params["country"] = country
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _fetch_job_details(self, slug: str, job_id: str) -> tuple[str, str]:
        """Return (posting_url, description_html) from the individual posting API."""
        url = f"{_API_BASE}/{slug}/postings/{job_id}"
        try:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            posting_url = data.get("postingUrl", "")
            sections = data.get("jobAd", {}).get("sections", {})
            parts = []
            for key in ("jobDescription", "qualifications", "additionalInformation"):
                text = sections.get(key, {}).get("text", "")
                if text:
                    parts.append(text)
            return posting_url, "\n".join(parts)
        except Exception as exc:
            _log.error(f"Could not fetch details for {job_id}: {exc}", exc_info=True)
            return "", ""

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        """Convert one raw SmartRecruiters job dict into a RawJob model."""
        try:
            job_id = raw.get("id", "")
            title = raw.get("name", "")
            location = raw.get("location", {}).get("city")
            country = raw.get("location", {}).get("countryCode")
            published_date = raw.get("publishedDate")

            if not title:
                return None

            posted_date = None
            if published_date:
                try:
                    posted_date = datetime.fromisoformat(published_date.replace("Z", "+00:00"))
                except (ValueError, OSError):
                    pass

            location_raw = location
            if country:
                location_raw = f"{location}, {country}" if location else country

            return RawJob(
                id=f"smartrecruiters:{job_id}",
                title=title,
                company=company.name,
                url="",  # populated after fetching individual posting
                location_raw=location_raw,
                posted_date=posted_date,
                description_html="",  # populated after fetching individual posting
                source="smartrecruiters",
                raw_payload=raw,
            )
        except Exception as e:
            _log.warning(f"Failed to normalize SmartRecruiters job: {e}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch and normalize job listings from SmartRecruiters.

        If company.career_url is set, it is treated as an ISO country code (e.g. 'il')
        and passed as ?country= to restrict results to that country.
        """
        if not company.slug:
            raise ValueError(f"SmartRecruiters scraper requires a slug for company: {company.name}")

        country = company.career_url or None  # e.g. "il" for Israel
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        all_jobs = []
        offset = 0
        limit = 100
        has_more = True

        while has_more:
            try:
                data = self._fetch_page(company.slug, offset=offset, limit=limit, country=country)
            except requests.HTTPError as e:
                _log.error(f"Error fetching SmartRecruiters jobs for {company.name}: {e}", exc_info=True)
                break

            raw_jobs = data.get("content", [])
            for raw in raw_jobs:
                job = self._normalize(raw, company)
                if not job:
                    continue
                if job.posted_date and job.posted_date < cutoff:
                    continue
                # Fetch individual posting for real URL + description
                time.sleep(self._delay)
                job_id = raw.get("id", "")
                posting_url, description_html = self._fetch_job_details(company.slug, job_id)
                job = job.model_copy(update={
                    "url": posting_url or job.url,
                    "description_html": description_html,
                })
                all_jobs.append(job)

            total = data.get("total") or data.get("totalFound", 0)
            has_more = offset + len(raw_jobs) < total
            offset += len(raw_jobs)

            if not has_more:
                break

        time.sleep(self._delay)
        return all_jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape SmartRecruiters job board")
    parser.add_argument("--company-id", required=True, help="SmartRecruiters company slug")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Output file path (JSON)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company_id,
        ats="smartrecruiters",
        slug=args.company_id,
        enabled=True,
    )

    scraper = SmartRecruitersScraper()
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