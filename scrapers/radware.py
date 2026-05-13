"""
Radware careers scraper.

Radware hosts jobs on radware.taleo.net — Oracle Taleo Enterprise ATS.
Direct HTTP requests bypass the PerfDrive/hCaptcha bot protection that
guards www.radware.com/Careers, but the Taleo domain itself is accessible.

API:
  GET  https://radware.taleo.net/careersection/ex/joblist.ftl  → seed session + hidden fields
  POST https://radware.taleo.net/careersection/ex/joblist.ajax → pipe-delimited job data

The AJAX response is a custom Taleo pipe-delimited format. Each job repeats
its fields several times; this scraper deduplicates by job_order_code.

Location codes: IL-IL-* = Israel, CO-CO-* = Colombia, etc.
Job URL: https://radware.taleo.net/careersection/ex/jobdetail.ftl?job={order_code}&lang=en

Standalone usage:
    python -m scrapers.radware --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Optional
import urllib.parse

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.radware")

_BASE = "https://radware.taleo.net/careersection/ex"
_LIST_URL = f"{_BASE}/joblist.ftl"
_AJAX_URL = f"{_BASE}/joblist.ajax"
_DETAIL_URL = f"{_BASE}/jobdetail.ftl"

_ISRAEL_PREFIX = "IL-IL-"


def _parse_taleo_jobs(text: str) -> list[dict]:
    """
    Parse Taleo AJAX pipe-delimited response.

    The response format repeats each job's fields multiple times.
    We find each job by matching: !{job_id}!|!{real_title}! ... !{location}!
    and deduplicate by job_order_code.
    """
    parts = text.split("!|!")
    seen_orders: dict[str, dict] = {}

    for i, part in enumerate(parts):
        part = part.strip()
        # Skip clearly non-job parts
        if not part or part in ("false", "true") or len(part) < 3:
            continue

        # Find location code pattern (XX-XX-City)
        if not re.match(r"^[A-Z]{2}-[A-Z]{2}-\S", part):
            continue

        location = part.rstrip("\xa0 ")
        # Job order code is the part immediately before the location
        if i < 1:
            continue
        job_order = parts[i - 1].strip()
        if not re.match(r"^[A-Z0-9]{7,10}$", job_order):
            continue

        # Find the title: look backwards past the job_order for a real title
        # The pattern is: ...!{title}!|!{job_id}!|!{title}!|!{job_id}!|...!{job_order}!|!{location}!
        title = ""
        for j in range(i - 2, max(0, i - 15), -1):
            candidate = parts[j].strip()
            # Real title: not a number-only string, not 'false', not a short ID
            if (len(candidate) > 5 and
                    not re.match(r"^\d+$", candidate) and
                    candidate not in ("false", "true", "Apply") and
                    not re.match(r"^[A-Z0-9]{6,10}$", candidate)):
                title = urllib.parse.unquote(candidate)
                break

        if not title:
            continue

        # Find posted date (right after schedule type)
        date = ""
        schedule = ""
        for j in range(i + 1, min(len(parts), i + 10)):
            p = parts[j].strip()
            if p in ("Full-time", "Part-time", "Contract", "Internship"):
                schedule = p
                if j + 1 < len(parts):
                    date_candidate = parts[j + 1].strip()
                    # Month Day, Year pattern
                    if re.match(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$", date_candidate):
                        date = date_candidate
                break

        if job_order not in seen_orders:
            seen_orders[job_order] = {
                "job_order": job_order,
                "title": title,
                "location": location,
                "schedule": schedule,
                "date": date,
            }
        elif title and not re.match(r"^\d+$", title):
            # Update with better title if we found one
            existing = seen_orders[job_order]["title"]
            if not existing or re.match(r"^\d+$", existing):
                seen_orders[job_order]["title"] = title

    return list(seen_orders.values())


class RadwareScraper(BaseScraper):
    """Scrapes Radware Israel jobs via Taleo ATS AJAX endpoint."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 20):
        self._delay = request_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": _LIST_URL,
            "Origin": "https://radware.taleo.net",
        })

    def _fetch_description_playwright(self, url: str) -> str:
        """Use Playwright to render a Taleo job detail page and extract description.

        Taleo renders content via JavaScript AJAX calls, so plain HTTP requests
        receive an empty template. Playwright waits for networkidle before
        extracting `span.text` elements (the description sections).
        """
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ))
                page.goto(url, wait_until="networkidle", timeout=self._timeout * 1000)
                page.wait_for_timeout(2000)

                spans = page.query_selector_all("span.text")
                parts = []
                for span in spans:
                    html = page.evaluate("el => el.innerHTML", span)
                    text = page.evaluate("el => el.innerText", span)
                    # Skip short metadata strings (location codes, IDs, etc.)
                    if text and len(text.strip()) > 50:
                        parts.append(html)

                browser.close()
                return "\n".join(parts)
        except Exception as exc:
            _log.error(f"Playwright error for {url}: {exc}", exc_info=True)
            return ""

    def _warm_session(self) -> dict:
        """GET the listing page to obtain session cookies and hidden form fields."""
        resp = self._session.get(_LIST_URL, timeout=self._timeout)
        resp.raise_for_status()
        hidden = dict(re.findall(
            r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
            resp.text,
        ))
        return hidden

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _fetch_ajax(self, hidden: dict) -> str:
        """POST the timezone-init AJAX call that returns the first batch of jobs."""
        data = {
            **hidden,
            "ftlpageid": "reqListAllJobsPage",
            "ftlinterfaceid": "requisitionListInterface",
            "ftlcompid": "validateTimeZoneId",
            "jsfCmdId": "validateTimeZoneId",
            "ftlcompclass": "InitTimeZoneAction",
            "ftlcallback": "requisition_restoreDatesValues",
            "ftlajaxid": "ftlx1",
            "tz": "GMT%2B00%3A00",
            "tzname": "UTC",
            "lang": "en",
        }
        resp = self._session.post(
            _AJAX_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest",
                     "Accept": "*/*"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.text

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch Radware Israel jobs from Taleo."""
        try:
            hidden = self._warm_session()
        except Exception as e:
            _log.error(f"Could not load Taleo page: {e}", exc_info=True)
            return []

        try:
            ajax_text = self._fetch_ajax(hidden)
        except Exception as e:
            _log.error(f"AJAX error: {e}", exc_info=True)
            return []

        raw_jobs = _parse_taleo_jobs(ajax_text)
        israel_jobs = [j for j in raw_jobs if j["location"].startswith(_ISRAEL_PREFIX)]

        jobs: list[RawJob] = []
        for raw in israel_jobs:
            title = raw["title"]
            if not title or title in ("false", "true"):
                continue
            job_order = raw["job_order"]
            url = f"{_DETAIL_URL}?job={job_order}&lang=en"
            city = raw["location"].replace(_ISRAEL_PREFIX, "").strip()
            # description_html is empty: Taleo detail pages populate content via
            # client-side JavaScript AJAX calls after page load. Plain HTTP requests
            # only receive an empty HTML template. Playwright would be needed to
            # render the full page and extract description.
            time.sleep(self._delay)
            description_html = self._fetch_description_playwright(url)
            jobs.append(RawJob(
                id=f"radware:{job_order}",
                title=title,
                company=company.name,
                url=url,
                location_raw=f"{city}, Israel",
                posted_date=None,
                description_html=description_html,
                source="radware",
                raw_payload=raw,
            ))

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Radware Israel jobs via Taleo")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    company = CompanyConfig(name="Radware", ats="radware", slug=None,
                            career_url=_LIST_URL, enabled=True)
    scraper = RadwareScraper()
    jobs = scraper.fetch_jobs(company)

    if args.verbose:
        print(f"Found {len(jobs)} Israel jobs", file=sys.stderr)
        for j in jobs:
            print(f"  {j.title} @ {j.location_raw}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
