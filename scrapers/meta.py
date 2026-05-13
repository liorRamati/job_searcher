"""
Meta Careers scraper using the GraphQL persisted-query API.

API endpoint:
  POST https://www.metacareers.com/api/graphql/
  — Uses Relay persisted queries (doc_id instead of full query text).
  — Requires LSD CSRF token extracted from the job search page.
  — Returns JSON with job listings under data.job_search_with_featured_jobs.all_jobs.
  — No authentication required (works without Facebook login).

The LSD token is a short-lived CSRF token embedded in the page HTML.
It is refreshed on every page load, so this scraper fetches it before
each job search call.

Supported search parameters (via search_input variable):
  q             : keyword search string (empty = all)
  offices       : list of office location strings e.g. ["Tel Aviv, Israel"]
  teams         : list of team display names
  roles         : list of role names
  divisions     : list of division names
  leadership_levels : list of leadership level names
  is_remote_only : bool
  sort_by_new   : bool (True = newest first)
  page          : page number (1-indexed)

Response structure:
  data.job_search_with_featured_jobs.all_jobs[]:
    id        : str  (numeric job ID, 16-19 digits)
    title     : str
    locations : list[str]
  data.job_search_filters.teams[]:
    team_display_name : str

Standalone usage:
    python -m scrapers.meta --company-id "Meta Israel" --offices "Tel Aviv, Israel" --verbose
    python -m scrapers.meta --company-id "Meta Israel" --offices "Tel Aviv, Israel" --output /tmp/meta_jobs.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper, build_search_query

_log = logging.getLogger("job_searcher.scrapers.meta")

# The Relay persisted query doc_id for the job search dropdown data query.
# This ID encodes the full GraphQL query on Meta's servers; only the doc_id
# and variables need to be sent in the request.
# Query name: CareersJobSearchInputDropdownDataQuery
# GraphQL field: job_search_with_featured_jobs(search_input: SearchInput)
_DOC_ID = "26228555073499023"

# URL to fetch the LSD CSRF token from (the job search page)
_TOKEN_URL = "https://www.metacareers.com/jobsearch"

# GraphQL endpoint as declared in the page's RelayAPIConfigDefaults config
_GRAPHQL_URL = "https://www.metacareers.com/api/graphql/"

# Job detail URL template
_JOB_URL_BASE = "https://www.metacareers.com/jobs/"

# Default search_input payload (all filters empty = return all jobs)
_DEFAULT_SEARCH_INPUT = {
    "q": "",
    "offices": [],
    "divisions": [],
    "roles": [],
    "leadership_levels": [],
    "saved_jobs": [],
    "saved_searches": [],
    "sub_teams": [],
    "teams": [],
    "is_leadership": False,
    "is_remote_only": False,
    "sort_by_new": False,
    "page": 1,
}


def _curl(args: list[str], timeout: int = 30) -> str:
    """Run a curl command and return stdout as a string.

    Uses subprocess to avoid Python's TLS fingerprint which Meta's servers
    reject with HTTP 400. curl passes the TLS handshake correctly.
    """
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"curl exited with code {result.returncode}: {result.stderr[:200]}"
        )
    return result.stdout


class MetaScraper(BaseScraper):
    """Scrapes job listings from Meta Careers via their GraphQL API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 30,
                 job_titles: list[str] = []):
        """
        Parameters
        ----------
        request_delay : seconds to sleep after each fetch call.
        timeout       : HTTP request timeout in seconds (passed to curl).
        job_titles    : list of target job titles used as keyword search query.
        """
        self._delay = request_delay
        self._timeout = timeout
        self._query = build_search_query(job_titles)

    def _get_lsd_token(self) -> str:
        """Fetch a fresh LSD CSRF token from the Meta job search page.

        The token is embedded in the page's inline JavaScript as:
          ["LSD",[],{"token":"<value>"}]

        Returns
        -------
        str : the LSD token value

        Raises
        ------
        RuntimeError : if the token cannot be extracted from the page
        """
        html = _curl([
            "curl", "-sL", "--tlsv1.2",
            "-A", (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", 'sec-ch-ua: "Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "-H", "sec-ch-ua-mobile: ?0",
            "-H", 'sec-ch-ua-platform: "Windows"',
            "-H", "Sec-Fetch-Site: none",
            "-H", "Sec-Fetch-Mode: navigate",
            "-H", "Sec-Fetch-User: ?1",
            "-H", "Sec-Fetch-Dest: document",
            _TOKEN_URL,
        ], timeout=self._timeout)

        match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"\}', html)
        if not match:
            raise RuntimeError(
                "Could not extract LSD token from Meta Careers page. "
                "The page structure may have changed."
            )
        return match.group(1)

    def _search(
        self,
        lsd_token: str,
        search_input: dict,
    ) -> list[dict]:
        """Execute a single job search GraphQL call.

        Parameters
        ----------
        lsd_token    : CSRF token from _get_lsd_token()
        search_input : dict following the _DEFAULT_SEARCH_INPUT structure

        Returns
        -------
        list[dict] : raw job dicts from data.job_search_with_featured_jobs.all_jobs
                     each dict has: id (str), title (str), locations (list[str])
        """
        variables = {"search_input": search_input}
        form_data = urlencode({
            "variables": json.dumps(variables),
            "doc_id": _DOC_ID,
            "lsd": lsd_token,
            "__a": "1",
            "__req": "a",
            "__hs": "20124.HYP:comet_pkg.2.1..0.0",
            "__dyn": "7xe6EaU2mcuf8eK3C5oe1eFxueT3oq6x5CAS",
            "__csr": "",
            "__comet_req": "1",
        })

        response_text = _curl([
            "curl", "-sL", "--tlsv1.2",
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", "Accept: */*",
            "-H", "X-FB-Friendly-Name: CareersJobSearchInputDropdownDataQuery",
            "-H", f"X-FB-LSD: {lsd_token}",
            "-H", "X-ASBD-ID: 129477",
            "-H", "Sec-Fetch-Dest: empty",
            "-H", "Sec-Fetch-Mode: cors",
            "-H", "Sec-Fetch-Site: same-origin",
            "-H", "Origin: https://www.metacareers.com",
            "-H", "Referer: https://www.metacareers.com/jobsearch",
            "-A", (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "--data-raw", form_data,
            _GRAPHQL_URL,
        ], timeout=self._timeout)

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Meta GraphQL returned non-JSON response "
                f"(first 200 chars): {response_text[:200]}"
            ) from exc

        if "errors" in data:
            raise RuntimeError(f"Meta GraphQL errors: {data['errors']}")

        return (
            data.get("data", {})
                .get("job_search_with_featured_jobs", {})
                .get("all_jobs", [])
        )

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        """Convert one raw API job dict into a RawJob model.

        Field mapping (Meta GraphQL -> RawJob):
          id             → "meta:{id}"
          title          → title
          locations[0]   → location_raw   (first location in the list)
          (none)         → posted_date    (Meta API does not return post dates)
          (none)         → description_html (not fetched at search time)

        Returns None if the job is malformed or missing required fields.
        """
        try:
            job_id = str(raw["id"])
            title = raw["title"].strip()
            locations = raw.get("locations", [])
            location_raw = locations[0] if locations else None

            return RawJob(
                id=f"meta:{job_id}",
                title=title,
                company=company.name,
                url=f"{_JOB_URL_BASE}{job_id}",
                location_raw=location_raw,
                posted_date=None,  # Meta API does not return post dates in search results
                description_html="",  # would require a separate request per job
                source="meta",
                raw_payload=raw,
            )
        except (KeyError, ValueError, TypeError) as exc:
            _log.warning(f"Skipping malformed job {raw.get('id')}: {exc}")
            return None

    def _fetch_description_playwright(self, url: str) -> str:
        """Use Playwright to render a Meta job page and extract description text.

        Meta job pages render via React. The description appears in the body text
        after the "Apply now" button. We extract everything that follows as the
        description.
        """
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ))
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)

                body = page.inner_text("body")
                browser.close()

                # Description follows "Apply now" button text
                lower = body.lower()
                apply_idx = lower.find("apply now")
                if apply_idx != -1:
                    return body[apply_idx + len("apply now"):].strip()
                return body
        except Exception as exc:
            _log.error(f"Playwright error for {url}: {exc}", exc_info=True)
            return ""

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch all jobs from Meta Careers for a given company config.

        The company's career_url is parsed for the `offices` query parameter to
        filter by location. Falls back to no location filter if not present.

        Note: Meta's search API does not return post dates, so max_age_days
        filtering is not applied (all matching jobs are returned).

        Parameters
        ----------
        company      : company config from companies.yaml
                       career_url may contain offices[0]=... query params
        max_age_days : not used (Meta API doesn't return post dates)
        """
        # Parse offices filter from career_url if present
        # e.g. https://www.metacareers.com/jobs?offices[0]=Tel%20Aviv%2C%20Israel
        offices: list[str] = []
        if company.career_url:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(company.career_url)
            qs = parse_qs(parsed.query)
            # offices[0], offices[1], ... query params
            for key, vals in qs.items():
                if key.startswith("offices"):
                    offices.extend(vals)

        search_input = {**_DEFAULT_SEARCH_INPUT, "offices": offices, "q": self._query}

        try:
            lsd_token = self._get_lsd_token()
        except Exception as exc:
            _log.error(f"Failed to get LSD token for {company.name}: {exc}", exc_info=True)
            return []

        try:
            raw_jobs = self._search(lsd_token, search_input)
        except Exception as exc:
            _log.error(f"GraphQL search failed for {company.name}: {exc}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for raw_job in raw_jobs:
            job = self._normalize(raw_job, company)
            if job is None:
                continue
            time.sleep(self._delay)
            description_html = self._fetch_description_playwright(job.url)
            if description_html:
                job = job.model_copy(update={"description_html": description_html})
            jobs.append(job)

        time.sleep(self._delay)
        return jobs


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scrape jobs from Meta Careers via GraphQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--company-id", required=True, help="Company display name")
    parser.add_argument(
        "--offices",
        action="append",
        default=[],
        help='Office location filter (repeatable), e.g. --offices "Tel Aviv, Israel"',
    )
    parser.add_argument("--output", help="Write JSON to this file (default: stdout)")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip HTTP calls and print what would be sent",
    )
    args = parser.parse_args(argv)

    offices = args.offices  # already a list from action="append"
    from urllib.parse import quote
    career_url = (
        "https://www.metacareers.com/jobs?"
        + "&".join(f"offices%5B{i}%5D={quote(o)}" for i, o in enumerate(offices))
        if offices
        else "https://www.metacareers.com/jobs"
    )

    company = CompanyConfig(
        name=args.company_id,
        ats="meta",
        slug=None,
        career_url=career_url,
        enabled=True,
    )

    scraper = MetaScraper()

    if args.dry_run:
        print(f"[dry-run] Would search Meta Careers with offices={offices}")
        print(f"[dry-run] POST {_GRAPHQL_URL}")
        print(f"[dry-run] doc_id={_DOC_ID}, variables.search_input.offices={offices}")
        return

    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}  [{job.url}]", file=sys.stderr)
        print(f"\nTotal: {len(jobs)} jobs", file=sys.stderr)

    output = json.loads(json.dumps([j.model_dump() for j in jobs], default=str))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
