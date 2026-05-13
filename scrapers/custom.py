"""
Custom scraper for career pages that don't use standard ATS platforms.

Uses Playwright to load public career pages and extract job listings.
This is a generic fallback for companies with custom career sites.

Note: Requires playwright to be installed and browsers to be initialized.
Run: playwright install chromium

Standalone usage:
    python -m scrapers.custom --company-id "Amazon Israel" --career-url "https://www.amazon.jobs/en/locations/tel-aviv-israel" --max-age-days 30 --verbose
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from models.job import CompanyConfig, RawJob
from scrapers.base import BaseScraper

_log = logging.getLogger("job_searcher.scrapers.custom")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class CustomScraper(BaseScraper):
    """Scrapes jobs from custom career pages using Playwright."""

    def __init__(self, request_delay: float = 1.0, timeout: int = 60, headless: bool = True):
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
        self._delay   = request_delay
        self._timeout = timeout
        self._headless = headless

    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch and normalize job listings from custom career page."""
        if not company.career_url:
            raise ValueError(f"Custom scraper requires a career_url for company: {company.name}")

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        jobs = []
        seen_urls = set()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self._headless)
                page = browser.new_page()
                page.set_default_timeout(self._timeout * 1000)

                page.goto(company.career_url)
                page.wait_for_load_state("networkidle")
                time.sleep(3)

                all_links = page.query_selector_all("a")
                
                job_link_patterns = [
                    r'/job/\d+',
                    r'/jobs/\d+',
                    r'jobId=',
                    r'/position/\d+',
                    r'/careers/\d+',
                    r'jobs\.microsoft\.com/job',
                    r'amazon\.jobs/job',
                    r'apple\.com/job/',
                ]

                for link in all_links:
                    try:
                        href = link.get_attribute("href")
                        if not href:
                            continue
                        
                        href_lower = href.lower()
                        
                        is_job_link = any(re.search(p, href_lower) for p in job_link_patterns)
                        if not is_job_link:
                            continue

                        if href in seen_urls:
                            continue
                        seen_urls.add(href)

                        title = link.inner_text().strip()
                        
                        if not title or len(title) < 3:
                            continue
                        
                        skip_words = ["careers", "about", "benefits", "culture", "learn more", "apply now", "locations", "teams", "search", "home", "faq", "contact", "blog", "news", "press"]
                        if any(skip in title.lower() for skip in skip_words):
                            continue

                        url = href
                        if not url.startswith("http"):
                            if "/" in company.career_url:
                                base = company.career_url.rsplit("/", 1)[0]
                                if href.startswith("/"):
                                    url = base + href
                                else:
                                    url = base + "/" + href

                        parent_text = ""
                        try:
                            parent = link.evaluate("el => el.parentElement?.innerText")
                            if parent:
                                parent_text = parent
                        except:
                            pass

                        location = None
                        location_keywords = ["Tel Aviv", "Haifa", "Jerusalem", "Petah Tikva", "Rehovot", "Israel", "Remote", "Hybrid", "Kfar Saba", "Herzliya", "Modi'in", "Israel", "HaSharon", "Netanya", "Be'er Sheva", " Herzliya", " Ra'anana"]
                        text_to_check = (title + " " + parent_text).lower()
                        for loc in location_keywords:
                            if loc.lower() in text_to_check:
                                location = loc
                                break

                        if len(title) > 150:
                            title = title[:150]

                        # Fetch description from the individual job page
                        description_html = ""
                        try:
                            job_page = browser.new_page()
                            job_page.set_default_timeout(self._timeout * 1000)
                            job_page.goto(url, wait_until="networkidle")
                            job_page.wait_for_timeout(2000)
                            description_html = job_page.inner_text("body")
                            job_page.close()
                        except Exception:
                            pass

                        job = RawJob(
                            id=f"custom:{len(jobs)+1}_{company.name.replace(' ', '_')}",
                            title=title,
                            company=company.name,
                            url=url,
                            location_raw=location,
                            posted_date=None,
                            description_html=description_html,
                            source="custom",
                            raw_payload={},
                        )

                        jobs.append(job)

                    except Exception:
                        continue

                browser.close()

        except Exception as e:
            _log.error(f"Error fetching jobs for {company.name}: {e}", exc_info=True)

        time.sleep(self._delay)
        return jobs


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape custom job board")
    parser.add_argument("--company-id", required=True, help="Company name")
    parser.add_argument("--career-url", required=True, help="Career page URL")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--output", help="Output file path (JSON)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    company = CompanyConfig(
        name=args.company_id,
        ats="custom",
        slug=None,
        career_url=args.career_url,
        enabled=True,
    )

    scraper = CustomScraper()
    jobs = scraper.fetch_jobs(company, max_age_days=args.max_age_days)

    if args.verbose:
        print(f"Found {len(jobs)} jobs from {company.name}")
        for job in jobs:
            print(f"  {job.title} @ {job.location_raw}")

    if args.output:
        import json
        with open(args.output, "w") as f:
            json.dump([job.model_dump() for job in jobs], f, indent=2, default=str)
        print(f"Wrote {len(jobs)} jobs to {args.output}")

    if not args.dry_run and not args.output:
        print(f"Found {len(jobs)} jobs (use --output to save)")


if __name__ == "__main__":
    import argparse
    main()