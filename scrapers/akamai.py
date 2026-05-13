"""
Akamai careers scraper (Israel).

Akamai hosts jobs via Oracle Fusion HCM at jobs.akamai.com. The Oracle HCM
REST API is publicly accessible without authentication.

API:
  GET https://fa-extu-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/
      recruitingCEJobRequisitions
      ?onlyData=true&expand=...
      &finder=findReqs;siteNumber=CX_1,...,locationId=300000000469279,...

The locationId 300000000469279 corresponds to Israel.

Response: {"items": [{"requisitionList": [{job objects}]}]}

Job URL: https://jobs.akamai.com/en/sites/CX_1/job/{Id}

Standalone usage:
    python -m scrapers.akamai --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.akamai")

_ORACLE_BASE = "https://fa-extu-saasfaprod1.fa.ocs.oraclecloud.com"
_SITE = "CX_1"
_ISRAEL_LOCATION_ID = "300000000469279"
_JOB_URL_BASE = "https://jobs.akamai.com/en/sites/CX_1/job"

_API_URL = (
    f"{_ORACLE_BASE}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    f"?onlyData=true"
    f"&expand=requisitionList.workLocation"
    f"&finder=findReqs;siteNumber={_SITE},"
    f"facetsList=LOCATIONS%3BTITLES%3BCATEGORIES%3BPOSTING_DATES,"
    f"limit=100,"
    f"locationId={_ISRAEL_LOCATION_ID},"
    f"sortBy=POSTING_DATES_DESC"
)


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class AkamaiScraper(BaseScraper):
    """Scrapes Akamai Israel jobs via Oracle Fusion HCM REST API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 20):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch(self) -> list[dict]:
        resp = self._session.get(_API_URL, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return []
        return items[0].get("requisitionList", [])

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            job_id = str(raw.get("Id", "")).strip()
            title = (raw.get("Title") or "").strip()
            if not title or not job_id:
                return None

            location = (raw.get("PrimaryLocation") or "Israel").strip()
            workplace = raw.get("WorkplaceType") or ""
            if workplace and workplace.lower() not in location.lower():
                location = f"{location} ({workplace})"

            description = raw.get("ShortDescriptionStr") or ""
            # Unescape HTML entities
            import html as html_mod
            description = html_mod.unescape(description)

            return RawJob(
                id=f"akamai:{job_id}",
                title=title,
                company=company.name,
                url=f"{_JOB_URL_BASE}/{job_id}",
                location_raw=location,
                posted_date=_parse_date(raw.get("PostedDate")),
                description_html=f"<p>{description}</p>" if description else "",
                source="akamai",
                raw_payload={
                    "Id": job_id,
                    "WorkplaceType": workplace,
                    "JobFunction": raw.get("JobFunction"),
                },
            )
        except Exception as e:
            _log.warning(f"Skipping malformed job: {e}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        try:
            raw_jobs = self._fetch()
        except Exception as e:
            _log.error(f"Error: {e}", exc_info=True)
            return []

        jobs = []
        for raw in raw_jobs:
            job = self._normalize(raw, company)
            if job:
                jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Akamai Israel jobs")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    company = CompanyConfig(name="Akamai Israel", ats="akamai", slug=None,
                            career_url=f"{_JOB_URL_BASE}", enabled=True)
    scraper = AkamaiScraper()
    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} jobs", file=sys.stderr)
        for j in jobs:
            print(f"  {j.title} @ {j.location_raw}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
