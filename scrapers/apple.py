"""
Apple Jobs scraper (Israel).

Apple embeds all search results as server-rendered JSON inside the page HTML
at https://jobs.apple.com/en-il/search — the Israel locale automatically
applies the country filter and returns ~140 Israel jobs per search.

The JSON lives in window.__staticRouterHydrationData under:
  loaderData.search.searchResults (list of job dicts)
  loaderData.search.totalRecords  (total count)

Job fields:
  positionId              → used in URL and as ID
  postingTitle            → job title
  postDateInGMT           → ISO 8601 posting timestamp
  locations               → list of {countryName, name (city), postLocationId, ...}
  team.teamName           → team/department
  transformedPostingTitle → URL slug

Job URL format:
  https://jobs.apple.com/en-us/details/{positionId}/{transformedPostingTitle}

Standalone usage:
    python -m scrapers.apple --verbose
"""

from __future__ import annotations

import argparse
import json
import re
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

_log = logging.getLogger("job_searcher.scrapers.apple")

_SEARCH_URL = "https://jobs.apple.com/en-il/search"
_JOB_BASE_URL = "https://jobs.apple.com/en-us/details"
_PAGE_SIZE = 20


class AppleScraper(BaseScraper):
    """Scrapes Apple Jobs Israel listings from server-rendered HTML."""

    def __init__(self, request_delay: float = 2.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=20),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_page(self, page: int) -> tuple[list[dict], int]:
        """Fetch one page. Returns (jobs_list, total_records)."""
        params = {} if page == 1 else {"page": page}
        resp = self._session.get(_SEARCH_URL, params=params, timeout=self._timeout)
        resp.raise_for_status()

        match = re.search(
            r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((.+?)\)(?:;|\n)',
            resp.text,
            re.DOTALL,
        )
        if not match:
            return [], 0

        # The value is a JSON-encoded string; parse twice
        outer = json.loads(match.group(1))
        data = json.loads(outer)

        search = data.get("loaderData", {}).get("search", {})
        results = search.get("searchResults", [])
        total = search.get("totalRecords", 0)
        return results, total

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            title = raw.get("postingTitle", "").strip()
            if not title:
                return None

            position_id = raw.get("positionId")
            if not position_id:
                return None

            slug = raw.get("transformedPostingTitle", "")
            url = f"{_JOB_BASE_URL}/{position_id}/{slug}" if slug else f"{_JOB_BASE_URL}/{position_id}"

            locations = raw.get("locations", [])
            if locations:
                loc_parts = [
                    loc.get("name") or loc.get("countryName", "")
                    for loc in locations
                    if loc
                ]
                location_raw = ", ".join(filter(None, loc_parts)) or None
            else:
                location_raw = None

            # postDateInGMT: "2026-04-29T14:22:14.221Z"
            posted_date = None
            gmt_str = raw.get("postDateInGMT", "")
            if gmt_str:
                try:
                    posted_date = datetime.fromisoformat(gmt_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            return RawJob(
                id=f"apple:{position_id}",
                title=title,
                company=company.name,
                url=url,
                location_raw=location_raw,
                posted_date=posted_date,
                description_html=raw.get("jobSummary", ""),
                source="apple",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from Apple Jobs."""
        cutoff = datetime.now(tz=timezone.utc).timestamp() - max_age_days * 86400

        all_jobs: list[RawJob] = []
        page = 1
        total = None

        while True:
            try:
                raw_jobs, page_total = self._fetch_page(page)
            except requests.HTTPError as e:
                _log.error(f"HTTP error page {page}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error page {page}: {e}", exc_info=True)
                break

            if total is None:
                total = page_total

            if not raw_jobs:
                break

            stop_early = False
            for raw in raw_jobs:
                job = self._normalize(raw, company)
                if not job:
                    continue
                if job.posted_date and job.posted_date.timestamp() < cutoff:
                    stop_early = True
                    continue
                all_jobs.append(job)

            if stop_early:
                break

            if total is not None and page * _PAGE_SIZE >= total:
                break

            page += 1
            time.sleep(self._delay)

        time.sleep(self._delay)
        return all_jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Apple Jobs Israel")
    parser.add_argument("--company", default="Apple Israel", help="Company name")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="apple",
        slug=None,
        career_url="https://jobs.apple.com/en-il/search",
        enabled=True,
    )

    scraper = AppleScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {args.company}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
