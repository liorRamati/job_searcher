"""
Palo Alto Networks Careers scraper (TalentBrew).

PAN's careers site (jobs.paloaltonetworks.com) runs on TalentBrew by Symphony Talent.
TalentBrew returns paginated HTML fragments inside a JSON envelope via a POST endpoint.
Job detail pages contain an application/ld+json block with the full description and
posting date.

API:
  POST https://jobs.paloaltonetworks.com/en/search-jobs/resultspost
  Content-Type: application/json
  Body: see _build_body() — Keywords="Israel" acts as the location filter;
        SearchResultsModuleName and SearchFiltersModuleName are required or the
        server returns empty HTML fragments.

Response envelope:
  {"results": "<section data-total-pages=N ...><ul>...</ul></section>",
   "filters": "...", "hasJobs": true, "hasContent": true}

Job detail (description + datePosted):
  GET https://jobs.paloaltonetworks.com/en/job/{city}/{slug}/47263/{id}
  → <script type="application/ld+json"> with @type=JobPosting

Standalone usage:
    python -m scrapers.paloalto --verbose
    python -m scrapers.paloalto --output /tmp/pan_jobs.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
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

_log = logging.getLogger("job_searcher.scrapers.paloalto")

_COMPANY_ID = "47263"
_SEARCH_URL = "https://jobs.paloaltonetworks.com/en/search-jobs/resultspost"
_JOB_BASE = "https://jobs.paloaltonetworks.com"
_PAGE_SIZE = 100  # TalentBrew accepts up to 100 results per page


class PaloAltoScraper(BaseScraper):
    """Scrapes Palo Alto Networks Israel jobs via the TalentBrew AJAX API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30,
                 job_titles: list[str] = []):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://jobs.paloaltonetworks.com/en/search-jobs/",
        })

    def _build_body(self, page: int) -> dict:
        # Keywords="Israel" is the location filter — TalentBrew matches it against
        # job location text (e.g. "Tel Aviv, Israel") rather than a structured field.
        # The two ModuleName fields are required; without them the server returns
        # an envelope with empty HTML fragments despite hasJobs=true.
        return {
            "Keywords": "Israel",
            "Location": "",
            "Distance": 50,
            "Latitude": "",
            "Longitude": "",
            "OrganizationIds": _COMPANY_ID,
            "CurrentPage": page,
            "RecordsPerPage": _PAGE_SIZE,
            "SortCriteria": 0,
            "SortDirection": 0,
            "SearchType": 1,
            "ResultsType": 0,
            "FacetType": 0,
            "FacetTerm": "",
            "ActiveFacetID": 0,
            "SearchResultsModuleName": "Section 29 - Search Results",
            "SearchFiltersModuleName": "Section 29 - Search Filters",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_page(self, page: int) -> dict:
        resp = self._session.post(
            _SEARCH_URL, json=self._build_body(page), timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _parse_results(self, html: str) -> tuple[list[dict], int]:
        """Parse the HTML fragment from response["results"].

        Returns (raw_jobs, total_pages).
        Each raw_job dict has: id, title, location, url.
        """
        soup = BeautifulSoup(html, "html.parser")
        section = soup.find("section", id="search-results")
        if not section:
            return [], 0

        total_pages = int(section.get("data-total-pages", 1))

        jobs = []
        for anchor in section.find_all("a", attrs={"data-job-id": True}):
            job_id = anchor["data-job-id"]
            href = anchor.get("href", "")
            url = f"{_JOB_BASE}{href}" if href else ""

            title_tag = anchor.find("h2", class_="section29__search-results-job-title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            loc_tag = anchor.find("span", class_="section29__result-location")
            location = loc_tag.get_text(strip=True) if loc_tag else ""

            if title and url:
                jobs.append({"id": job_id, "title": title, "location": location, "url": url})

        return jobs, total_pages

    def _fetch_description(self, url: str) -> tuple[str, Optional[datetime]]:
        """Fetch job detail page; extract description and datePosted from ld+json."""
        try:
            resp = self._session.get(
                url, timeout=self._timeout, headers={"Accept": "text/html"}
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            ld = soup.find("script", type="application/ld+json")
            if ld and ld.string:
                data = json.loads(ld.string)
                description = data.get("description", "")
                raw_date = data.get("datePosted")
                posted_date = None
                if raw_date:
                    try:
                        posted_date = datetime.strptime(
                            raw_date, "%Y-%m-%d"
                        ).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                return description, posted_date
        except Exception as exc:
            _log.warning(f"Could not fetch description from {url}: {exc}")
        return "", None

    def _normalize(
        self,
        raw: dict,
        company: CompanyConfig,
        description: str,
        posted_date: Optional[datetime],
    ) -> Optional[RawJob]:
        try:
            return RawJob(
                id=f"paloalto:{raw['id']}",
                title=raw["title"],
                company=company.name,
                url=raw["url"],
                location_raw=raw.get("location") or None,
                posted_date=posted_date,
                description_html=description,
                source="paloalto",
                raw_payload=raw,
            )
        except Exception as exc:
            _log.warning(f"Skipping malformed job: {exc}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Israel jobs from Palo Alto Networks via TalentBrew."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        # Collect all listing stubs across pages first, then fetch descriptions.
        all_raw: list[dict] = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            try:
                data = self._fetch_page(page)
            except requests.HTTPError as e:
                _log.error(f"HTTP error fetching page {page}: {e}", exc_info=True)
                break
            except Exception as e:
                _log.error(f"Error fetching page {page}: {e}", exc_info=True)
                break

            html = data.get("results", "")
            if not html:
                _log.warning(f"Empty results HTML on page {page} — stopping pagination")
                break

            raw_jobs, total_pages = self._parse_results(html)
            if not raw_jobs:
                break

            all_raw.extend(raw_jobs)
            _log.debug(f"Page {page}/{total_pages}: {len(raw_jobs)} jobs")
            page += 1
            time.sleep(self._delay)

        _log.debug(f"Fetched {len(all_raw)} job stubs; fetching descriptions...")

        jobs: list[RawJob] = []
        for raw in all_raw:
            time.sleep(self._delay)
            description, posted_date = self._fetch_description(raw["url"])
            if posted_date and posted_date < cutoff:
                continue
            job = self._normalize(raw, company, description, posted_date)
            if job:
                jobs.append(job)

        return jobs


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Palo Alto Networks Israel jobs via TalentBrew",
        epilog=__doc__,
    )
    parser.add_argument("--company", default="Palo Alto Networks Israel")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Write JSON to this file (default: stdout)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company,
        ats="paloalto",
        slug=None,
        career_url="https://jobs.paloaltonetworks.com",
        enabled=True,
    )

    scraper = PaloAltoScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {company.name}", file=sys.stderr)
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}  [{job.url}]", file=sys.stderr)

    output = [j.model_dump() for j in jobs]
    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
