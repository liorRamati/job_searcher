"""
Workday job board scraper using the undocumented REST API.

Workday exposes a JSON API at:
  POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
  — No authentication required for public job boards.
  — Returns paginated JSON with job listings.

The API URL is derived from the career_url in companies.yaml:
  career_url: https://{tenant}.wd{N}.myworkdayjobs.com/{board}
  api_url:    https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs

Standalone usage:
    python -m scrapers.workday --career-url "https://intel.wd1.myworkdayjobs.com/External" --verbose
    python -m scrapers.workday --career-url "https://paypal.wd1.myworkdayjobs.com/jobs" --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

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
from scrapers.base import BaseScraper, build_search_query

_log = logging.getLogger("job_searcher.scrapers.workday")

_PAGE_SIZE = 20  # Workday rejects requests with limit > 20


def _parse_career_url(career_url: str) -> tuple[str, str, int, str, Optional[str]]:
    """
    Parse a Workday career URL into its components.

    Returns (base_url, tenant, wd_num, board, search_text)
    e.g. 'https://intel.wd1.myworkdayjobs.com/External'
      -> ('https://intel.wd1.myworkdayjobs.com', 'intel', 1, 'External', None)

    An optional ?q=TEXT suffix adds a searchText filter to each API request:
      'https://salesforce.wd12.myworkdayjobs.com/External_Career_Site?q=Tel+Aviv'
      -> (..., 'Tel Aviv')
    """
    from urllib.parse import parse_qs
    parsed = urlparse(career_url)
    host = parsed.netloc  # e.g. 'intel.wd1.myworkdayjobs.com'
    path = parsed.path.lstrip("/")  # e.g. 'External'
    qs = parse_qs(parsed.query)
    search_text = qs.get("q", [None])[0]

    m = re.match(r'^([\w-]+)\.(wd(\d+))\.myworkdayjobs\.com$', host, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse Workday URL host: {host}")

    tenant = m.group(1)
    wd_num = int(m.group(3))
    board = path or "jobs"
    base_url = f"https://{host}"
    return base_url, tenant, wd_num, board, search_text


def _posted_on_to_date(posted_on: str) -> Optional[datetime]:
    """Convert Workday's relative 'Posted N Days Ago' string to a datetime."""
    if not posted_on:
        return None
    text = posted_on.lower()
    if "today" in text or "just posted" in text:
        return datetime.now(timezone.utc)
    m = re.search(r'(\d+)\s+days?\s+ago', text)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    m = re.search(r'(\d+)\s+months?\s+ago', text)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)) * 30)
    return None


class WorkdayScraper(BaseScraper):
    """Scrapes jobs from Workday via the undocumented public REST API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30,
                 job_titles: list[str] = []):
        self._delay = request_delay
        self._timeout = timeout
        self._query = build_search_query(job_titles)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_page(self, api_url: str, offset: int, limit: int,
                    search_text: Optional[str] = None) -> dict:
        """Fetch one page of jobs from the Workday API."""
        body: dict = {"limit": limit, "offset": offset}
        if search_text:
            body["searchText"] = search_text
        resp = self._session.post(api_url, json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _fetch_description(self, job_url: str) -> str:
        """Extract job description from Workday job page via application/ld+json."""
        try:
            resp = self._session.get(job_url, timeout=self._timeout,
                                     headers={"Accept": "text/html"})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            ld = soup.find("script", type="application/ld+json")
            if ld and ld.string:
                data = json.loads(ld.string)
                return data.get("description", "")
        except Exception as exc:
            _log.error(f"Could not fetch description from {job_url}: {exc}", exc_info=True)
        return ""

    def _normalize(self, raw: dict, company: CompanyConfig, base_url: str,
                   board: str) -> Optional[RawJob]:
        """Convert one Workday job dict into a RawJob."""
        try:
            title = raw.get("title", "").strip()
            if not title:
                return None

            external_path = raw.get("externalPath", "")
            # Workday web URL: {base_url}/en-US/{board}{external_path}
            url = f"{base_url}/en-US/{board}{external_path}" if external_path else ""

            # Extract job ID from the path (last path segment after the last underscore)
            job_id = ""
            if external_path:
                m = re.search(r'_([A-Z0-9-]+)$', external_path)
                job_id = m.group(1) if m else external_path.split("/")[-1]

            location_raw = raw.get("locationsText") or None
            posted_on = raw.get("postedOn", "")
            posted_date = _posted_on_to_date(posted_on)

            return RawJob(
                id=f"workday:{job_id or title[:30]}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=posted_date,
                description_html="",
                source="workday",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch and normalize job listings from Workday."""
        if not company.career_url:
            raise ValueError(f"Workday scraper requires a career_url for: {company.name}")

        try:
            base_url, tenant, wd_num, board, search_text = _parse_career_url(company.career_url)
        except ValueError as e:
            _log.error(f"{e}", exc_info=True)
            return []

        api_url = f"{base_url}/wday/cxs/{tenant}/{board}/jobs"
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        # URL-level ?q= takes priority (e.g. Salesforce uses it as a location filter).
        # Fall back to the job_titles query only when no URL-level search text is set.
        effective_search = search_text or self._query or None

        all_jobs: list[RawJob] = []
        offset = 0

        while True:
            try:
                data = self._fetch_page(api_url, offset=offset, limit=_PAGE_SIZE,
                                        search_text=effective_search)
            except requests.HTTPError as e:
                _log.error(f"HTTP error for {company.name}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error for {company.name}: {e}", exc_info=True)
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            for raw in postings:
                job = self._normalize(raw, company, base_url, board)
                if job is None:
                    continue
                if job.posted_date and job.posted_date < cutoff:
                    continue
                # Fetch description from job detail page via LD+JSON
                time.sleep(self._delay)
                description_html = self._fetch_description(job.url)
                if description_html:
                    job = job.model_copy(update={"description_html": description_html})
                all_jobs.append(job)

            total = data.get("total", 0)
            offset += len(postings)
            if offset >= total:
                break

        time.sleep(self._delay)
        return all_jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Workday job board via REST API")
    parser.add_argument("--career-url", required=True, help="Workday career page URL")
    parser.add_argument("--company-name", default=None)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Output file path (JSON)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company_name or args.career_url,
        ats="workday",
        slug=None,
        career_url=args.career_url,
        enabled=True,
    )

    scraper = WorkdayScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {company.name}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}", file=sys.stderr)

    if args.output:
        import json
        with open(args.output, "w") as f:
            json.dump([job.model_dump() for job in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        import json
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
