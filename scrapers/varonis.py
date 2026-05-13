"""
Varonis career site scraper.

Varonis uses a custom career site at https://careers.varonis.com/ backed by
Jobvite ATS. The public REST API returns all active requisitions; each job's
canonical URL and description come from Jobvite.

API endpoints:
  GET https://careers.varonis.com/api/getRequisitions
    — Returns all active job requisitions as JSON.
    — Response: {"data": [{eId, title, locationCity, locationCountry, department,
                           jobLocations: [{jobDetailsUrl, applyUrl, country, ...}], ...}], ...}

  Job URL: jobLocations[n]["jobDetailsUrl"]
    e.g. https://app.jobvite.com/CompanyJobs/Job.aspx?j={eId}&l={locEId}
    (redirects to https://jobs.jobvite.com/careers/varonis/job/{eId})

  Description: fetched from the Jobvite job detail page
    selector: div.jv-job-detail-description

Standalone usage:
    python -m scrapers.varonis --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

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

_log = logging.getLogger("job_searcher.scrapers.varonis")

_API_URL = "https://careers.varonis.com/api/getRequisitions"
_BASE_URL = "https://careers.varonis.com"
_ISRAEL_KEYWORDS = {"israel", "il", "tel aviv", "raanana", "herzliya", "petah tikva"}


def _is_israel(location_str: str) -> bool:
    s = location_str.lower()
    return any(kw in s for kw in _ISRAEL_KEYWORDS)


class VaronisScraper(BaseScraper):
    """Scrapes Varonis's custom career REST API (Jobvite-backed)."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/html",
            "Referer": "https://careers.varonis.com/",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_raw(self) -> list[dict]:
        resp = self._session.get(_API_URL, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def _best_location_entry(self, job_locations: list[dict]) -> Optional[dict]:
        """Return the Israel location entry if present, otherwise the first entry."""
        for loc in job_locations:
            country = (loc.get("country") or loc.get("name") or "").lower()
            if _is_israel(country):
                return loc
        return job_locations[0] if job_locations else None

    def _fetch_description(self, job_details_url: str) -> str:
        """Fetch description from the Jobvite job detail page."""
        try:
            resp = self._session.get(job_details_url, timeout=self._timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            desc_div = soup.select_one("div.jv-job-detail-description")
            if desc_div:
                return desc_div.decode_contents().strip()
        except Exception as exc:
            _log.error(f"Could not fetch description from {job_details_url}: {exc}", exc_info=True)
        return ""

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            eid = raw.get("eId", "")
            title = raw.get("title", "").strip()
            if not title:
                return None

            job_locations = raw.get("jobLocations") or []
            loc_entry = self._best_location_entry(job_locations) if job_locations else None

            # URL: use the Jobvite job details URL from the API response
            if loc_entry and loc_entry.get("jobDetailsUrl"):
                url = loc_entry["jobDetailsUrl"]
            else:
                # Fallback: construct Jobvite URL from eId
                url = f"https://jobs.jobvite.com/careers/varonis/job/{eid}"

            # Location
            city = raw.get("locationCity") or (loc_entry or {}).get("name") or ""
            country = raw.get("locationCountry") or (loc_entry or {}).get("country") or ""
            location_parts = [p for p in [city, country] if p and p.lower() != "none"]
            location_raw = ", ".join(location_parts) or None

            return RawJob(
                id=f"varonis:{eid}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=None,
                description_html="",   # populated after dedup in fetch_jobs
                source="varonis",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch all jobs from Varonis career API and populate descriptions."""
        try:
            raw_jobs = self._fetch_raw()
        except requests.HTTPError as e:
            _log.error(f"HTTP error: {e}", exc_info=True)
            return []
        except Exception as e:
            _log.error(f"Error: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for raw in raw_jobs:
            job = self._normalize(raw, company)
            if not job:
                continue

            # Fetch description from the Jobvite job detail page
            time.sleep(self._delay)
            description_html = self._fetch_description(job.url)
            job = job.model_copy(update={"description_html": description_html})
            jobs.append(job)

        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Varonis career site")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name="Varonis",
        ats="varonis",
        slug=None,
        career_url=_API_URL,
        enabled=True,
    )

    scraper = VaronisScraper()
    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from Varonis", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw} | {job.url}", file=sys.stderr)
            if job.description_html:
                print(f"    desc: {job.description_html[:100]}...", file=sys.stderr)

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
