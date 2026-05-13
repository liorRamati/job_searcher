"""
Greenhouse public job board scraper.

API endpoint:
  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  — No authentication required.
  — Returns all active job postings for the company in a single JSON response.
  — The `content=true` parameter includes the full job description HTML.

Greenhouse is the most common ATS among Israeli tech companies, so this scraper
handles the majority of companies in config/companies.yaml.

Standalone usage:
    python -m scrapers.greenhouse --company-id taboola --max-age-days 30 --verbose
    python -m scrapers.greenhouse --company-id jfrog --output /tmp/jfrog_jobs.json
    python -m scrapers.greenhouse --company-id testco --dry-run --verbose
"""

from __future__ import annotations

import argparse
import json
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
    wait_random,
)


def wait_with_retry_after(retry_state):
    """Custom wait function that respects Retry-After header if present."""
    attempt = retry_state.attempt_number
    if attempt is None:
        return 2

    response = getattr(retry_state, 'outcome', None)
    if response and hasattr(response, 'exception'):
        exc = response.exception()
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after:
                try:
                    return int(retry_after)
                except ValueError:
                    pass

    return min(2 ** attempt, 10)

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.greenhouse")

# Base URL for the Greenhouse public boards API.
# Full URL: {_API_BASE}/{slug}/jobs?content=true
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseScraper(BaseScraper):
    """Scrapes all active jobs from a Greenhouse-hosted job board."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        """
        Parameters
        ----------
        request_delay : seconds to sleep after each company's API call.
                        Prevents rate-limiting when scraping multiple companies.
        timeout       : HTTP request timeout in seconds.
        """
        self._delay   = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        # Identify ourselves to the server — avoids being blocked as a headless bot.
        self._session.headers["User-Agent"] = "JobSearchAgent/1.0"

    @retry(
        # Retry up to 3 times on HTTP errors (e.g. 429 Too Many Requests, 5xx).
        # Greenhouse's API is reliable but rate limits can hit during batch scraping.
        stop=stop_after_attempt(3),
        # Exponential backoff with Retry-After header support
        wait=wait_with_retry_after,
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_raw(self, slug: str) -> dict:
        """Fetch the raw JSON response from the Greenhouse API for one company."""
        url  = f"{_API_BASE}/{slug}/jobs"
        resp = self._session.get(url, params={"content": "true"}, timeout=self._timeout)
        resp.raise_for_status()  # raises HTTPError on 4xx/5xx, triggering the retry
        return resp.json()

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        """
        Convert one raw API job dict into a RawJob model.

        Returns None if any required field is missing so the caller can skip
        malformed entries without crashing the entire scrape.

        Field mapping (Greenhouse API -> RawJob):
          id            → "greenhouse:{id}"   (prefixed for global uniqueness)
          title         → title
          location.name → location_raw
          updated_at    → posted_date         (updated_at is the most recent date)
          absolute_url  → url                 (normalized by RawJob field_validator)
          content       → description_html    (raw HTML from the job board)
        """
        try:
            job_id      = str(raw["id"])
            # Prefer updated_at; fall back to first_published if updated_at is missing.
            updated_raw = raw.get("updated_at") or raw.get("first_published")
            posted_date: Optional[datetime] = None
            if updated_raw:
                # Greenhouse returns ISO 8601 with 'Z' suffix; replace it for fromisoformat()
                posted_date = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))

            return RawJob(
                id=f"greenhouse:{job_id}",
                title=raw["title"],
                company=company.name,
                url=raw["absolute_url"],
                location_raw=(raw.get("location") or {}).get("name"),
                posted_date=posted_date,
                description_html=raw.get("content") or "",
                source="greenhouse",
                # Store everything except the full HTML in raw_payload for debugging.
                # Excluding 'content' keeps the payload small (HTML can be 10+ KB).
                raw_payload={k: v for k, v in raw.items() if k != "content"},
            )
        except (KeyError, ValueError, TypeError) as exc:
            _log.warning(f"Skipping malformed job {raw.get('id')}: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """
        Fetch, normalize, and age-filter all active jobs for one company.

        Age filtering uses the job's `updated_at` date. Jobs older than max_age_days
        are silently dropped — they're unlikely to still be accepting applications.

        Parameters
        ----------
        company      : company configuration from companies.yaml
        max_age_days : exclude jobs last updated more than this many days ago
        """
        if not company.slug:
            raise ValueError(f"Company '{company.name}' has no Greenhouse slug configured")

        data   = self._fetch_raw(company.slug)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        jobs: list[RawJob] = []
        for raw_job in data.get("jobs", []):
            job = self._normalize(raw_job, company)
            if job is None:
                continue  # malformed entry — already logged
            # Skip jobs older than the cutoff window
            if job.posted_date and job.posted_date < cutoff:
                continue
            jobs.append(job)

        # Polite delay before the next company — prevents hitting rate limits
        time.sleep(self._delay)
        return jobs


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def _load_dry_run_fixture() -> dict:
    """Return the bundled test fixture instead of hitting the live API."""
    import os
    fixture = os.path.join(
        os.path.dirname(__file__), "..", "tests", "fixtures", "greenhouse_raw_response.json"
    )
    with open(fixture) as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scrape jobs from a Greenhouse job board",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--company-id",   required=True, help="Greenhouse board slug")
    parser.add_argument("--company-name", default=None,  help="Display name (defaults to slug)")
    parser.add_argument("--output",       help="Write JSON to this file (default: stdout)")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--verbose",  action="store_true")
    parser.add_argument("--dry-run",  action="store_true", help="Use fixture data, no HTTP calls")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company_name or args.company_id,
        ats="greenhouse",
        slug=args.company_id,
        enabled=True,
    )

    scraper = GreenhouseScraper()

    if args.dry_run:
        # Monkey-patch _fetch_raw to return fixture data without making any HTTP calls.
        # This is the same pattern used in tests (see tests/test_greenhouse.py).
        stub = _load_dry_run_fixture()
        scraper._fetch_raw = lambda slug: stub  # type: ignore[method-assign]

    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        for job in jobs:
            date_str = job.posted_date.date().isoformat() if job.posted_date else "unknown"
            print(f"  [{date_str}] {job.title} @ {job.location_raw}", file=sys.stderr)
        print(f"\nTotal: {len(jobs)} jobs", file=sys.stderr)

    output = json.loads(json.dumps([j.model_dump() for j in jobs], default=str))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
