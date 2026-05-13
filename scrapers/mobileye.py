"""
Mobileye career site scraper.

Mobileye hosts their careers at https://careers.mobileye.com/jobs using a
Nuxt.js static-site-generator setup. All job listings are pre-rendered in the
HTML — no browser needed.

Job URL format: https://careers.mobileye.com/jobs/{title-slug}/{uuid}

Description: fetched from each individual job page by extracting long HTML
strings from the __NUXT_DATA__ JSON array embedded in the page. The Nuxt
array stores job content (intro, responsibilities, requirements) as HTML
fragments; all strings longer than 200 characters are joined to form the
full description.

Standalone usage:
    python -m scrapers.mobileye --verbose
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

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.mobileye")

_JOBS_URL = "https://careers.mobileye.com/jobs"
_BASE_URL = "https://careers.mobileye.com"
_NUXT_DESC_MIN_LEN = 200   # strings shorter than this are IDs/labels, not content


class MobileyeScraper(BaseScraper):
    """Scrapes Mobileye's pre-rendered career page."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def _fetch_html(self, url: str) -> str:
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.text

    def _extract_nuxt_description(self, html: str) -> str:
        """Extract job description from __NUXT_DATA__ embedded JSON array.

        Nuxt SSG stores all page data in a flat JSON array. Job description
        content (intro paragraph, responsibilities, requirements) shows up as
        HTML strings longer than ~200 characters. We collect all such strings
        and join them in document order.
        """
        nuxt_m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not nuxt_m:
            return ""
        try:
            nuxt_arr = json.loads(nuxt_m.group(1))
        except (json.JSONDecodeError, ValueError):
            return ""

        fragments = [
            x for x in nuxt_arr
            if isinstance(x, str) and len(x) >= _NUXT_DESC_MIN_LEN
        ]
        if not fragments:
            return ""

        # Wrap each fragment in a <div> and join; the scorer strips HTML anyway
        return "\n".join(f"<div>{frag}</div>" for frag in fragments)

    def _extract_jobs_from_html(self, html: str, company: CompanyConfig) -> list[dict]:
        """Return list of {slug, uuid} dicts from the listing page HTML."""
        seen: set[str] = set()
        results = []

        # Extract href patterns like /jobs/{slug}/{uuid}
        href_pattern = re.compile(r'/jobs/([^/"\s]+)/([a-f0-9-]{36})', re.IGNORECASE)
        for slug, uuid in href_pattern.findall(html):
            if uuid in seen:
                continue
            seen.add(uuid)
            results.append({"slug": slug, "uuid": uuid})

        return results

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch all jobs from Mobileye's pre-rendered career page."""
        try:
            listing_html = self._fetch_html(_JOBS_URL)
        except requests.HTTPError as e:
            _log.error(f"HTTP error fetching listing: {e}", exc_info=True)
            return []
        except Exception as e:
            _log.error(f"Error fetching listing: {e}", exc_info=True)
            return []

        stubs = self._extract_jobs_from_html(listing_html, company)
        if not stubs:
            _log.warning("No job URLs found in listing page")
            return []

        jobs: list[RawJob] = []
        for stub in stubs:
            slug = stub["slug"]
            uuid = stub["uuid"]
            url = f"{_BASE_URL}/jobs/{slug}/{uuid}"
            title = slug.replace("-", " ").title()

            # Fetch individual job page for description
            time.sleep(self._delay)
            try:
                job_html = self._fetch_html(url)
                description_html = self._extract_nuxt_description(job_html)
            except Exception as e:
                _log.error(f"Could not fetch {url}: {e}", exc_info=True)
                description_html = ""

            jobs.append(RawJob(
                id=f"mobileye:{uuid}",
                title=title,
                company=company.name,
                url=url,
                location_raw=None,
                posted_date=None,
                description_html=description_html,
                source="mobileye",
                raw_payload={"slug": slug, "uuid": uuid},
            ))

        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Mobileye career site")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name="Mobileye",
        ats="mobileye",
        slug=None,
        career_url=_JOBS_URL,
        enabled=True,
    )

    scraper = MobileyeScraper()
    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from Mobileye", file=sys.stderr)
        for job in jobs:
            has_desc = "✓ desc" if job.description_html else "✗ no desc"
            print(f"  {has_desc} | {job.title} @ {job.url}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
