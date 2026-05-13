"""
Eightfold.ai career site scraper (pcsx API).

Several Israeli-relevant tech companies host their career pages on Eightfold.ai,
which exposes a public JSON search API at /api/pcsx/search.

Known companies using this platform:
  - Qualcomm  → https://careers.qualcomm.com   (domain=qualcomm.com)
  - Microsoft → https://apply.careers.microsoft.com (domain=microsoft.com)
  - Amdocs    → https://jobs.amdocs.com         (domain=amdocs.com)

API format:
  GET {base_url}/api/pcsx/search
  Params: domain, query, location, start, num
  Response: {"status": 200, "data": {"positions": [{id, name, locations, postedTs, positionUrl, ...}]}}

Standalone usage:
    python -m scrapers.eightfold --verbose
"""

from __future__ import annotations

import argparse
import json
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
from scrapers.base import BaseScraper, build_search_query

_log = logging.getLogger("job_searcher.scrapers.eightfold")

_PAGE_SIZE = 20


class EightfoldScraper(BaseScraper):
    """Scrapes career pages powered by Eightfold.ai via the pcsx JSON API."""

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
    def _fetch_page(self, base_url: str, domain: str, location: str, start: int) -> list[dict]:
        resp = self._session.get(
            f"{base_url}/api/pcsx/search",
            params={
                "domain": domain,
                "query": self._query,
                "location": location,
                "start": start,
                "num": _PAGE_SIZE,
            },
            headers={"Referer": f"{base_url}/"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("positions", []) or []

    def _fetch_description(self, url: str) -> str:
        """Extract job description from Eightfold job page via application/ld+json."""
        try:
            resp = self._session.get(url, timeout=self._timeout,
                                     headers={"Accept": "text/html"})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            ld = soup.find("script", type="application/ld+json")
            if ld and ld.string:
                data = json.loads(ld.string)
                return data.get("description", "")
        except Exception as exc:
            _log.error(f"Could not fetch description from {url}: {exc}", exc_info=True)
        return ""

    def _normalize(self, raw: dict, company: CompanyConfig, base_url: str) -> Optional[RawJob]:
        try:
            job_id = str(raw.get("id", ""))
            title = raw.get("name", "").strip()
            if not title:
                return None

            locations = raw.get("locations") or []
            location_raw = "; ".join(locations) if locations else None

            position_url = raw.get("positionUrl", "")
            if position_url:
                url = f"{base_url}{position_url}" if position_url.startswith("/") else position_url
            else:
                url = f"{base_url}/careers/job/{job_id}"

            # postedTs is a Unix timestamp in seconds
            posted_ts = raw.get("postedTs")
            posted_date = datetime.fromtimestamp(posted_ts, tz=timezone.utc) if posted_ts else None

            department = raw.get("department", "")

            return RawJob(
                id=f"eightfold:{job_id}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=posted_date,
                description_html="",
                source="eightfold",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch jobs for one company from its Eightfold.ai career portal."""
        career_url = company.career_url or ""
        # career_url format: "https://careers.qualcomm.com|qualcomm.com|Israel"
        # or just the base URL (domain auto-derived, location defaults to Israel)
        parts = career_url.split("|")
        base_url = parts[0].rstrip("/")
        domain = parts[1] if len(parts) > 1 else base_url.split("//", 1)[-1].split("/")[0]
        location = parts[2] if len(parts) > 2 else "Israel"

        cutoff = datetime.now(tz=timezone.utc).timestamp() - max_age_days * 86400

        all_jobs: list[RawJob] = []
        start = 0
        while True:
            try:
                positions = self._fetch_page(base_url, domain, location, start)
            except requests.HTTPError as e:
                _log.error(f"HTTP error for {company.name}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error for {company.name}: {e}", exc_info=True)
                break

            if not positions:
                break

            for raw in positions:
                job = self._normalize(raw, company, base_url)
                if not job:
                    continue
                posted_ts = raw.get("postedTs")
                if posted_ts and posted_ts < cutoff:
                    continue
                # Fetch description from individual job page
                time.sleep(self._delay)
                description_html = self._fetch_description(job.url)
                if description_html:
                    job = job.model_copy(update={"description_html": description_html})
                all_jobs.append(job)

            if len(positions) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE
            time.sleep(self._delay)

        time.sleep(self._delay)
        return all_jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape an Eightfold.ai career site")
    parser.add_argument("--base-url", required=True, help="Base URL, e.g. https://careers.qualcomm.com")
    parser.add_argument("--domain", required=True, help="Domain filter, e.g. qualcomm.com")
    parser.add_argument("--location", default="Israel", help="Location filter (default: Israel)")
    parser.add_argument("--company", default="Company", help="Company name")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="eightfold",
        slug=None,
        career_url=f"{args.base_url}|{args.domain}|{args.location}",
        enabled=True,
    )

    scraper = EightfoldScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=365)

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
