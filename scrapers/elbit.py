"""
Elbit Systems careers scraper.

Elbit hosts jobs on elbitsystemscareer.com — a Next.js app backed by the
Niloo AI platform (niloo-server.herokuapp.com). The public endpoint returns
all Israeli job listings as a JSON array without authentication.

API:
  POST https://niloo-server.herokuapp.com/actions-elbit
  Content-Type: application/json
  Body: {"cmd": "get-jobs"}
  Response: list of job objects

Job fields used:
  jobId         → numeric ID; URL = https://elbitsystemscareer.com/job/?jid={jobId}
  jobTitle      → Hebrew job title
  jobCode       → internal requirement ID
  area          → Israeli area: Center, North, Sharon, Shfela, South, Jerusalem Area
  description   → HTML-encoded full description (HTML entities, needs unescaping)
  openDate      → ISO 8601 date string

Note: Heroku free-tier cold start may add ~30 s to the first request. All jobs
are Israeli (Elbit is an Israeli defense/tech company). The description field
contains the full job text — individual detail pages do not need to be fetched.

Standalone usage:
    python -m scrapers.elbit --verbose
    python -m scrapers.elbit --verbose --area Center,Sharon,Shfela
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import logging

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.elbit")

_API_URL = "https://niloo-server.herokuapp.com/actions-elbit"
_JOB_BASE = "https://elbitsystemscareer.com/job/"

# Location ID → Hebrew city name (from embedded JS on the careers page)
_CITY_MAP: dict[int, str] = {
    85: "Ashdod", 104: "Kiryat Shmona", 106: "Har Yona (Park HiTec Bar Lev)",
    109: "Airport City", 129: "Tel Hai", 131: "Hybrid", 134: "Beer Sheva",
    149: "Bnei Brak", 416: "Haifa", 443: "Hutsarim", 467: "Holon",
    524: "Karmiel", 628: "Lod", 812: "Nes Tsiona", 816: "Netanya",
    878: "Ofakim", 935: "Caesarea", 960: "Ra'anana", 964: "Ramat Gan",
    966: "Ramat HaSharon", 975: "Ramla", 992: "Rehovot", 1008: "Rosh HaAyin",
    1050: "Sderot", 1137: "Tel Aviv", 1203: "Yavne", 2191: "Nof HaGalil",
    2966: "Yokneam", 8: "Modi'in", 72: "Arad",
}


def _city_name(city_id: Optional[int]) -> str:
    if city_id is None:
        return ""
    return _CITY_MAP.get(city_id, str(city_id))


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class ElbitScraper(BaseScraper):
    """Scrapes Elbit Systems jobs via the Niloo backend API."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 40):
        self._delay = request_delay
        self._timeout = timeout  # Long for Heroku cold start
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Referer": "https://elbitsystemscareer.com/",
            "Origin": "https://elbitsystemscareer.com",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=5, max=15),
        retry=retry_if_exception_type((requests.HTTPError, requests.Timeout)),
        reraise=True,
    )
    def _fetch_all(self) -> list[dict]:
        resp = self._session.post(_API_URL, json={"cmd": "get-jobs"}, timeout=self._timeout)
        resp.raise_for_status()
        result = resp.json()
        if not isinstance(result, list):
            return []
        return result

    def _normalize(self, raw: dict, company: CompanyConfig) -> Optional[RawJob]:
        try:
            job_id = raw.get("jobId")
            title = (raw.get("jobTitle") or "").strip()
            if not title or not job_id:
                return None

            area = (raw.get("area") or "").strip().rstrip()
            city_id = raw.get("cityId")
            city = _city_name(city_id)
            location_raw = f"{city}, Israel" if city else f"{area}, Israel" if area else "Israel"

            description_encoded = raw.get("description") or ""
            requirements_encoded = raw.get("requirements") or ""
            desc_html = html.unescape(description_encoded)
            if requirements_encoded:
                desc_html += "<br/><h4>Requirements</h4>" + html.unescape(requirements_encoded)

            return RawJob(
                id=f"elbit:{job_id}",
                title=title,
                company=company.name,
                url=f"{_JOB_BASE}?jid={job_id}",
                location_raw=location_raw,
                posted_date=_parse_date(raw.get("openDate")),
                description_html=desc_html,
                source="elbit",
                raw_payload={
                    "jobId": job_id,
                    "jobCode": raw.get("jobCode"),
                    "area": area,
                    "cityId": city_id,
                    "employerName": raw.get("employerName"),
                },
            )
        except Exception as e:
            _log.warning(f"Skipping malformed job: {e}")
            return None

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch all Elbit jobs from the Niloo API."""
        try:
            raw_jobs = self._fetch_all()
        except requests.Timeout:
            _log.error("Timeout — Niloo server cold start; try again in ~30s")
            return []
        except Exception as e:
            _log.error(f"Error: {e}", exc_info=True)
            return []

        jobs: list[RawJob] = []
        for raw in raw_jobs:
            job = self._normalize(raw, company)
            if job:
                jobs.append(job)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape Elbit Systems careers")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--area", help="Comma-separated areas to filter (e.g. Center,Sharon)")
    parser.add_argument("--output", help="Output file path (JSON)")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name="Elbit Systems",
        ats="elbit",
        slug=None,
        career_url="https://elbitsystemscareer.com/jobs/",
        enabled=True,
    )

    scraper = ElbitScraper()
    jobs = scraper.fetch_jobs(company)

    if args.area:
        filter_areas = {a.strip().lower() for a in args.area.split(",")}
        jobs = [j for j in jobs if any(a in (j.location_raw or "").lower() for a in filter_areas)]

    if args.verbose:
        print(f"Found {len(jobs)} jobs", file=sys.stderr)
        for j in jobs:
            print(f"  {j.title} @ {j.location_raw}", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump([j.model_dump() for j in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}", file=sys.stderr)
    else:
        print(json.dumps([j.model_dump() for j in jobs], indent=2, default=str))


if __name__ == "__main__":
    main()
