"""
Tower Semiconductor careers scraper (Israel).

Tower Semiconductor lists Israel jobs on a WordPress career site. The Israel
page at /our-loactions/israel/ contains an HTML list of job titles linking to
individual job-description pages (?job_id=N). Job-description pages require a
warm session (visit homepage first) to bypass Cloudflare WPEngine protection.

Flow:
  1. GET careers.towersemi.com/ — warm up Cloudflare session cookie
  2. GET /our-loactions/israel/ — parse job title + job_id pairs
  3. For each job: GET /job-description?job_id=N — extract description text

Job URL: https://careers.towersemi.com/job-description?job_id={job_id}

Standalone usage:
    python -m scrapers.towersemi --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.towersemi")

_BASE = "https://careers.towersemi.com"
_ISRAEL_PAGE = f"{_BASE}/our-loactions/israel/"
_JOB_URL = f"{_BASE}/job-description?job_id={{job_id}}"


def _extract_text(html: str) -> str:
    """Strip scripts, styles, and tags; normalize whitespace."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&[a-z#0-9]+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()


class TowerSemiScraper(BaseScraper):
    """Scrapes Tower Semiconductor Israel jobs from the careers WordPress site."""

    def __init__(self, request_delay: float = 1.5, timeout: int = 20):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        })

    def _warm_session(self) -> None:
        """Visit homepage to get Cloudflare cookie before fetching job pages."""
        self._session.get(_BASE + "/", timeout=self._timeout)
        time.sleep(0.5)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _get(self, url: str) -> str:
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.text

    def _fetch_israel_jobs(self) -> list[tuple[str, str]]:
        """Parse the Israel page and return [(job_id, title)] pairs."""
        html = self._get(_ISRAEL_PAGE)
        # Links like: <a href="/job-description?job_id=9430">Automation Software Developer</a>
        matches = re.findall(
            r'<a[^>]+href="(/job-description\?job_id=(\d+))"[^>]*>([^<]+)</a>',
            html, re.IGNORECASE,
        )
        # Deduplicate by job_id (links sometimes appear twice on the page)
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for _, job_id, title in matches:
            if job_id not in seen:
                seen.add(job_id)
                result.append((job_id, title.strip()))
        return result

    def _fetch_description(self, job_id: str) -> str:
        """Fetch the job-description page and return plain-text body."""
        try:
            html = self._get(_JOB_URL.format(job_id=job_id))
            return _extract_text(html)
        except Exception as e:
            _log.error(f"Could not fetch job {job_id}: {e}", exc_info=True)
            return ""

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel job listings from Tower Semiconductor career site."""
        try:
            self._warm_session()
            job_list = self._fetch_israel_jobs()
        except Exception as e:
            _log.error(f"Error fetching Israel page: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for job_id, title in job_list:
            if not title:
                continue

            # Fetch detail page for description
            desc_text = self._fetch_description(job_id)
            time.sleep(self._delay)

            # Extract location from description text
            loc_match = re.search(r"Location:\s*([^,\n]+)", desc_text)
            location = loc_match.group(1).strip() if loc_match else "Israel"

            # Build description HTML from plain text
            desc_html = f"<p>{desc_text[:3000]}</p>" if desc_text else ""

            jobs.append(RawJob(
                id=f"towersemi:{job_id}",
                title=title,
                company=company.name,
                url=_JOB_URL.format(job_id=job_id),
                location_raw=location,
                posted_date=None,
                description_html=desc_html,
                source="towersemi",
                raw_payload={"job_id": job_id},
            ))

        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Tower Semiconductor Israel jobs")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name="Tower Semiconductor", ats="towersemi", slug=None,
        career_url=_ISRAEL_PAGE, enabled=True,
    )
    scraper = TowerSemiScraper()
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
