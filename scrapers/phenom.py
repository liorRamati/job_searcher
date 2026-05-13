"""
PhenomPeople careers scraper (Thales, Cisco, and similar).

PhenomPeople powers many large enterprise career sites. Job data is accessible
via a public POST /widgets endpoint that returns JSON.

API:
  POST https://{careers_domain}/widgets
  Content-Type: application/json
  Body: PhenomPeople search widget payload with selected_fields: {country: ["Israel"]}

Response: {"refineSearch": {"hits": N, "totalHits": T, "data": {"jobs": [...]}}}

Job fields used:
  title, reqId, city, country, applyUrl, postedDate, descriptionTeaser,
  cityStateCountry, multi_category

career_url format in companies.yaml:
  "{base_url}|{pageId}|{optional_refNum}"
  e.g. "https://careers.thalesgroup.com|page18"
       "https://careers.cisco.com|page4|CISCISGLOBAL"

Standalone usage:
    python -m scrapers.phenom --company "Thales Israel" \\
        --base-url https://careers.thalesgroup.com --page-id page18 --verbose
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
from scrapers.base import BaseScraper, build_search_query

_log = logging.getLogger("job_searcher.scrapers.phenom")

_PAGE_SIZE = 10


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _job_url(base_url: str, req_id: str) -> str:
    """Construct the canonical job detail URL from the PhenomPeople base and reqId."""
    base = base_url.rstrip("/")
    return f"{base}/global/en/job/{req_id}"


class PhenomScraper(BaseScraper):
    """Scrapes PhenomPeople-powered career sites (Thales, Cisco, etc.)."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 20,
                 job_titles: list[str] = []):
        self._delay = request_delay
        self._timeout = timeout
        self._query = build_search_query(job_titles)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _build_payload(self, page_id: str, ref_num: Optional[str],
                       offset: int, size: int) -> dict:
        payload: dict = {
            "lang": "en_global", "deviceType": "desktop", "country": "global",
            "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "",
            "subsearch": "", "from": offset, "jobs": True, "counts": True,
            "all_fields": ["category", "country", "state", "city", "type",
                           "workerSubType", "workLocation"],
            "size": size, "clearAll": False, "jdsource": "facets",
            "isSliderEnable": False, "pageId": page_id, "siteType": "external",
            "keywords": self._query, "global": True,
            "selected_fields": {"country": ["Israel"]},
            "locationData": {},
        }
        if ref_num:
            payload["refNum"] = ref_num
        return payload

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_page(self, widgets_url: str, page_id: str, ref_num: Optional[str],
                    offset: int) -> dict:
        payload = self._build_payload(page_id, ref_num, offset, _PAGE_SIZE)
        resp = self._session.post(widgets_url, json=payload, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _normalize(self, raw: dict, company: CompanyConfig,
                   base_url: str) -> Optional[RawJob]:
        try:
            req_id = raw.get("reqId") or raw.get("jobId", "")
            title = (raw.get("title") or "").strip()
            if not title or not req_id:
                return None

            city = raw.get("city") or raw.get("cityState") or ""
            country = raw.get("country") or "Israel"
            location_raw = raw.get("cityStateCountry") or f"{city}, {country}".strip(", ")

            apply_url = raw.get("applyUrl") or ""
            job_url = _job_url(base_url, req_id)

            description = raw.get("descriptionTeaser") or ""
            category = ", ".join(raw.get("multi_category") or [])

            return RawJob(
                id=f"phenom:{req_id}",
                title=title,
                company=company.name,
                url=job_url,
                location_raw=location_raw,
                posted_date=_parse_date(raw.get("postedDate")),
                description_html=f"<p>{description}</p>" if description else "",
                source="phenom",
                raw_payload={
                    "reqId": req_id, "category": category,
                    "applyUrl": apply_url, "city": city,
                },
            )
        except Exception as e:
            _log.warning(f"Skipping malformed job: {e}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from a PhenomPeople career site."""
        if not company.career_url:
            raise ValueError(f"PhenomScraper requires career_url for {company.name}")

        # Parse career_url: "base_url|pageId|optionalRefNum"
        parts = company.career_url.split("|")
        base_url = parts[0].rstrip("/")
        page_id = parts[1] if len(parts) > 1 else "page1"
        ref_num = parts[2] if len(parts) > 2 else None
        widgets_url = f"{base_url}/widgets"

        jobs: list[RawJob] = []
        offset = 0

        try:
            first_page = self._fetch_page(widgets_url, page_id, ref_num, 0)
        except Exception as e:
            _log.error(f"{company.name}: {e}", exc_info=True)
            return []

        result = first_page.get("refineSearch", {})
        total = result.get("totalHits", 0)
        raw_jobs = result.get("data", {}).get("jobs", [])

        for raw in raw_jobs:
            job = self._normalize(raw, company, base_url)
            if job:
                jobs.append(job)

        offset += len(raw_jobs)
        time.sleep(self._delay)

        while offset < total:
            try:
                page = self._fetch_page(widgets_url, page_id, ref_num, offset)
            except Exception as e:
                _log.error(f"{company.name} page offset={offset}: {e}", exc_info=True)
                break
            result = page.get("refineSearch", {})
            raw_jobs = result.get("data", {}).get("jobs", [])
            if not raw_jobs:
                break
            for raw in raw_jobs:
                job = self._normalize(raw, company, base_url)
                if job:
                    jobs.append(job)
            offset += len(raw_jobs)
            time.sleep(self._delay)

        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape PhenomPeople career site")
    parser.add_argument("--company", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--ref-num", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    career_url = f"{args.base_url}|{args.page_id}"
    if args.ref_num:
        career_url += f"|{args.ref_num}"

    company = CompanyConfig(
        name=args.company, ats="phenom", slug=None,
        career_url=career_url, enabled=True,
    )
    scraper = PhenomScraper()
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
