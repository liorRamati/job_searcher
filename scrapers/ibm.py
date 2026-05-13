"""
IBM Careers scraper (Israel).

IBM's career page embeds a Next.js search widget that calls a public Elasticsearch
API on page load. The API can be called directly via POST without session cookies.

API:
  POST https://www-api.ibm.com/search/api/v2
  Content-Type: application/json
  Body: Elasticsearch DSL with post_filter on field_keyword_05=Israel

Response fields:
  hits.total.value   = total job count
  hits.hits[*]._source.title   = job title
  hits.hits[*]._source.url     = job URL (https://careers.ibm.com/careers/JobDetail?jobId=N)
  hits.hits[*]._source.description = job description text

Note: The API uses size=30 and returns all Israel jobs in a single response
(IBM currently has ~3 Israel openings). No pagination is needed.

Standalone usage:
    python -m scrapers.ibm --verbose
"""

from __future__ import annotations

import argparse
import json
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

_log = logging.getLogger("job_searcher.scrapers.ibm")

_API_URL = "https://www-api.ibm.com/search/api/v2"
_SEARCH_BODY = {
    "appId": "careers",
    "scopes": ["careers2"],
    "query": {"bool": {"must": []}},
    "post_filter": {"term": {"field_keyword_05": "Israel"}},
    "size": 100,
    "sort": [{"_score": "desc"}, {"pageviews": "desc"}],
    "lang": "zz",
    "localeSelector": {},
    "sm": {"query": "", "lang": "zz"},
    "_source": ["_id", "title", "url", "description", "language",
                "field_keyword_17", "field_keyword_08", "field_keyword_18"],
}


class IBMScraper(BaseScraper):
    """Scrapes IBM Careers Israel via the public Elasticsearch REST API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.ibm.com/",
            "Content-Type": "application/json",
            "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="120", "Chromium";v="120"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_all(self) -> list[dict]:
        resp = self._session.post(_API_URL, json=_SEARCH_BODY, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json().get("hits", {}).get("hits", [])

    def _normalize(self, hit: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            src = hit.get("_source", {})
            title = src.get("title", "").strip()
            if not title:
                return None

            url = src.get("url", "")
            if not url:
                return None

            # Extract jobId from URL for stable ID
            job_id = hit.get("_id", url.rstrip("/").split("?jobId=")[-1] if "jobId=" in url else url.split("/")[-1])

            description = src.get("description", "")
            location_raw = "Israel"  # All results are filtered to Israel

            return RawJob(
                id=f"ibm:{job_id}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=None,  # API doesn't return posting date
                description_html=f"<p>{description}</p>" if description else "",
                source="ibm",
                raw_payload=src,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from IBM Careers."""
        try:
            hits = self._fetch_all()
        except requests.HTTPError as e:
            _log.error(f"HTTP error: {e}", exc_info=True)
            return []
        except Exception as e:
            _log.error(f"Error: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for hit in hits:
            job = self._normalize(hit, company)
            if job:
                jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape IBM Careers Israel")
    parser.add_argument("--company", default="IBM Israel", help="Company name")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="ibm",
        slug=None,
        career_url="https://www.ibm.com/careers/search?field_keyword_05[]=Israel",
        enabled=True,
    )

    scraper = IBMScraper()
    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {args.company}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}", file=sys.stderr)
            print(f"    {job.url}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
