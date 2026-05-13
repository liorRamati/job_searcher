"""
Google Sheets writer.

Appends qualified jobs as rows to a configured spreadsheet.
Column order matches HEADERS below — do not reorder without updating both.

Standalone usage:
    python -m output.google_sheets --input scored_jobs.json --dry-run --verbose
    python -m output.google_sheets --input scored_jobs.json \\
        --spreadsheet-id 1BxiM... --credentials config/google_credentials.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

from models.job import RawJob

_log = logging.getLogger("job_searcher.sheets")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Canonical column order — new columns should be appended to the right.
HEADERS = [
    "Date Added",        # A
    "Company",           # B
    "Job Title",         # C
    "Link",              # D  ← deduplication key (URL)
    "Location",          # E
    "Work Type",         # F
    "Description",       # G
    "Requirements",      # H
    "Tech Stack Found",  # I
    "Score",             # J
    "Score Breakdown",   # K
    "Translated",        # L
    "Cover Letter",      # M
]

_LINK_COLUMN_INDEX = 4  # 1-based: column D

_HYBRID_RE = re.compile(r"\bhybrid\b|partially remote|flexible work", re.IGNORECASE)
_ONSITE_RE = re.compile(r"\bon.?site\b|in.?office", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)

# Matches common requirements section headers appearing on their own line.
_REQUIREMENTS_SECTION_RE = re.compile(
    r"(?:^|\n)[ \t]*("
    r"requirements?|qualifications?|what you(?:'ll)? need|what we(?:'re)? looking for|"
    r"must.?have|skills? required|minimum qualifications?|basic qualifications?|"
    r"preferred qualifications?|job requirements?|your qualifications?|about you|"
    r"who you are|your background|experience(?: & | and )skills?|required skills?|"
    r"technical requirements?|we(?:'re)? looking for|your profile|candidate profile"
    r")[ \t]*:?[ \t]*(?:\n|$)",
    re.IGNORECASE,
)


def _extract_work_type(job: RawJob) -> str:
    combined = (job.description_html or "") + " " + (job.location_raw or "")
    if _HYBRID_RE.search(combined):
        return "hybrid"
    if _ONSITE_RE.search(combined):
        return "on-site"
    if _REMOTE_RE.search(combined):
        return "remote"
    return "unknown"


def strip_html(html: str) -> str:
    """Strip HTML tags and return clean plain text (no truncation)."""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def extract_requirements(plain_text: str) -> str:
    """
    Heuristically extract the requirements/qualifications section from plain text.
    Returns the section text, or empty string if no section header is found.
    """
    match = _REQUIREMENTS_SECTION_RE.search(plain_text)
    if match:
        return plain_text[match.start():].strip()
    return ""


def _job_to_row(job: RawJob, timestamp: str) -> list:
    work_type = _extract_work_type(job)
    plain_text = strip_html(job.description_html) if job.description_html else ""

    # Use LLM-extracted requirements if present, otherwise fall back to heuristic.
    requirements = getattr(job, "requirements_text", "") or ""
    if not requirements and plain_text:
        requirements = extract_requirements(plain_text)

    score = getattr(job, "score", "")
    score_breakdown = json.dumps(getattr(job, "score_breakdown", {})) if getattr(job, "score_breakdown", {}) else ""
    tech_stack = ", ".join(getattr(job, "tech_stack_found", []) or [])
    cover_letter = getattr(job, "cover_letter", "") or ""

    return [
        timestamp,
        job.company,
        job.title,
        job.url,
        job.location_raw or "",
        work_type,
        plain_text,
        requirements,
        tech_stack,
        score,
        score_breakdown,
        "Yes" if job.translated else "",
        cover_letter,
    ]


class SheetsWriter:
    def __init__(self, credentials_path: str, spreadsheet_id: str, sheet_name: str):
        try:
            creds = Credentials.from_service_account_file(credentials_path, scopes=_SCOPES)
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key(spreadsheet_id)
        except Exception as exc:
            _log.error(f"Failed to connect to Google Sheets (id={spreadsheet_id}): {exc}", exc_info=True)
            raise

        try:
            self._ws = spreadsheet.worksheet(sheet_name)
            _log.debug(f"Opened existing worksheet '{sheet_name}'")
        except gspread.WorksheetNotFound:
            self._ws = spreadsheet.add_worksheet(
                title=sheet_name, rows=5000, cols=len(HEADERS)
            )
            _log.info(f"Created new worksheet '{sheet_name}'")

        self._ensure_headers()

    def _ensure_headers(self) -> None:
        first_row = self._ws.row_values(1)
        if not first_row:
            self._ws.append_row(HEADERS, value_input_option="RAW")
            _log.debug("Wrote column headers to new sheet")

    def load_existing_urls(self) -> set[str]:
        """Read column D (Link) to build the deduplication index."""
        try:
            values = self._ws.col_values(_LINK_COLUMN_INDEX)
        except Exception as exc:
            _log.error(f"Failed to read existing URLs from Sheets: {exc}", exc_info=True)
            return set()
        urls = {v.strip() for v in values if v and v.strip() != "Link"}
        _log.debug(f"Loaded {len(urls)} existing job URLs for deduplication")
        return urls

    def append_jobs(self, jobs: list[RawJob]) -> None:
        if not jobs:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [_job_to_row(job, now) for job in jobs]
        try:
            self._ws.append_rows(rows, value_input_option="RAW")
            _log.info(f"Wrote {len(rows)} job rows to Google Sheets")
        except Exception as exc:
            _log.error(f"Failed to write {len(rows)} rows to Google Sheets: {exc}", exc_info=True)
            raise


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Append scored jobs to Google Sheets",
        epilog=__doc__,
    )
    parser.add_argument("--input", required=True, help="JSON array of (Scored)Job objects")
    parser.add_argument("--spreadsheet-id", help="Google Sheets ID (or set GOOGLE_SPREADSHEET_ID)")
    parser.add_argument("--sheet-name", default="Jobs")
    parser.add_argument("--credentials", default="config/google_credentials.json")
    parser.add_argument("--min-score", type=float, default=60.0, help="Only write jobs >= this score")
    parser.add_argument("--audit-output", help="Write a summary JSON of what was written")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print rows, don't write to Sheets")
    args = parser.parse_args(argv)

    with open(args.input) as f:
        raw = json.load(f)

    # Accept both RawJob and ScoredJob dicts
    jobs = [RawJob(**j) for j in raw if (j.get("score", 100) or 100) >= args.min_score]

    if args.verbose:
        print(f"Loaded {len(raw)} jobs, {len(jobs)} at or above min_score={args.min_score}", file=sys.stderr)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = [_job_to_row(job, now) for job in jobs]

    if args.dry_run:
        print(f"DRY RUN — {len(rows)} rows would be written:\n")
        for row in rows:
            print(f"  {row[1]}: {row[2]} @ {row[4]} (score={row[9] or 'N/A'})")
    else:
        spreadsheet_id = args.spreadsheet_id or os.environ.get("GOOGLE_SPREADSHEET_ID")
        if not spreadsheet_id:
            print("ERROR: provide --spreadsheet-id or set GOOGLE_SPREADSHEET_ID", file=sys.stderr)
            sys.exit(1)
        writer = SheetsWriter(args.credentials, spreadsheet_id, args.sheet_name)
        writer.append_jobs(jobs)
        print(f"Wrote {len(jobs)} rows to Google Sheets")

    if args.audit_output:
        audit = [{"company": r[1], "title": r[2], "url": r[3]} for r in rows]
        with open(args.audit_output, "w") as f:
            json.dump(audit, f, indent=2)


if __name__ == "__main__":
    main()
