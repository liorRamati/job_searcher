#!/usr/bin/env python3
"""
Job Search Agent — main entry point.

Pipeline:
  1. Load config, user profile, and locations.
  2. Optionally validate config without making any API calls (--validate).
  3. Optionally parse a resume PDF/Word file to auto-generate profile.json (--parse-resume).
  4. For each enabled company: scrape → translate → deduplicate → disqualify → score.
  5. Optionally apply LLM filtering for more accurate relevance classification (--llm-filter).
  6. Optionally generate cover letters for top-scoring jobs (--generate-cover-letters).
  7. Write qualifying ScoredJobs to Google Sheets (skipped with --dry-run).

Scoring modes:
  Without --llm-filter: strict scorer. Jobs must have a relevant title keyword AND score ≥
    score_threshold to appear in results. Fast and deterministic.
  With --llm-filter: loose pre-scorer + LLM. The scorer uses a lower threshold (min_score_before_llm)
    and relaxed title requirements, feeding a wider candidate pool to the LLM for final classification.
    Slower but more accurate for borderline roles.

Usage:
    python main.py                              # full run, writes to Sheets
    python main.py --dry-run                    # prints results, no Sheets write
    python main.py --dry-run --verbose          # show every job and why it was filtered
    python main.py --validate                   # validate config without making any API calls
    python main.py --companies taboola jfrog    # limit to specific company slugs
    python main.py --llm-filter                 # use LLM for relevance filtering
    python main.py --parse-resume resume.pdf    # parse resume to generate config/profile.json
    python main.py --generate-cover-letters     # generate cover letters for top jobs
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()


def load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_config(config: dict, companies_cfg: dict) -> list[str]:
    """
    Validate configuration without making any API calls.

    Returns list of validation errors (empty if valid).
    """
    errors = []

    if not companies_cfg.get("companies"):
        errors.append("No companies defined in config/companies.yaml")

    enabled_companies = [c for c in companies_cfg.get("companies", []) if c.get("enabled")]
    if not enabled_companies:
        errors.append("No companies are enabled in config/companies.yaml")

    for company in enabled_companies:
        ats = company.get("ats")
        if ats in ("greenhouse", "lever", "smartrecruiters", "gem") and not company.get("slug"):
            errors.append(f"{ats} company '{company.get('name')}' missing slug")
        if ats in ("workday", "custom", "eightfold") and not company.get("career_url"):
            errors.append(f"{ats} company '{company.get('name')}' missing career_url")
        if ats == "comeet" and not company.get("slug"):
            errors.append(f"Comeet company '{company.get('name')}' missing slug (must be 'uid:token')")

    agent_config = config.get("agent", {})
    if not agent_config.get("score_threshold"):
        errors.append("Missing agent.score_threshold in settings.yaml")

    search_config = config.get("search", {})
    if not search_config.get("max_job_age_days"):
        errors.append("Missing search.max_job_age_days in settings.yaml")

    gs_config = config.get("google_sheets", {})
    if not gs_config.get("credentials_file"):
        errors.append("Missing google_sheets.credentials_file in settings.yaml")
    if not gs_config.get("sheet_name"):
        errors.append("Missing google_sheets.sheet_name in settings.yaml")

    return errors


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Job Search Agent")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument(
        "--companies",
        nargs="*",
        metavar="SLUG",
        help="Limit run to these Greenhouse slugs (e.g. --companies taboola jfrog)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Google Sheets")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate config without making any API calls",
    )
    parser.add_argument(
        "--parse-resume",
        metavar="RESUME_FILE",
        help="Parse a resume (PDF/Word) to generate user profile JSON",
    )
    parser.add_argument(
        "--generate-cover-letters",
        action="store_true",
        help="Generate cover letters for qualified jobs (requires LLM)",
    )
    parser.add_argument(
        "--llm-filter",
        action="store_true",
        help="Use LLM for job filtering (more accurate, slower). Without this flag, uses strict rule-based filtering.",
    )
    args = parser.parse_args(argv)

    # ── Load config files ──────────────────────────────────────────────────────
    config        = load_yaml(args.config)
    companies_cfg = load_yaml("config/companies.yaml")
    locations     = load_yaml("config/locations.yaml")
    tech_keywords = load_yaml("config/tech_keywords.yaml") or {}

    max_age_days:    int = config["search"]["max_job_age_days"]
    score_threshold: int = config["agent"]["score_threshold"]
    creds_path:      str = config["google_sheets"]["credentials_file"]
    sheet_name:      str = config["google_sheets"]["sheet_name"]

    if args.validate:
        print("Validating configuration...")
        errors = validate_config(config, companies_cfg)
        if errors:
            print("\nValidation FAILED:")
            for err in errors:
                print(f"  ✗ {err}")
            sys.exit(1)
        else:
            print("Validation PASSED")
            enabled = [c["name"] for c in companies_cfg.get("companies", []) if c.get("enabled")]
            print(f"  Enabled companies: {len(enabled)}")
            print(f"  Score threshold: {score_threshold}")
            print(f"  Max job age: {max_age_days} days")
            sys.exit(0)

    from logging_config import setup_logging
    log_dir = config.get("logging", {}).get("log_dir", "logs")
    # When --verbose: lower console handler to DEBUG so per-job detail appears on
    # screen. File handler is always DEBUG, so verbose detail goes to the log
    # regardless of this flag.
    console_level = "DEBUG" if args.verbose else "INFO"
    logger = setup_logging(log_dir=log_dir, console_level=console_level)
    logger.info(f"Starting job search — dry_run={args.dry_run}, verbose={args.verbose}")
    # GOOGLE_SPREADSHEET_ID env var overrides the YAML value (useful in CI/CD)
    spreadsheet_id: str = (
        os.environ.get("GOOGLE_SPREADSHEET_ID") or config["google_sheets"]["spreadsheet_id"]
    )

    # ── Import pipeline modules ────────────────────────────────────────────────
    # Deferred imports keep --help fast and make it easy to run individual modules
    # without needing all deps installed.
    from models.job import CompanyConfig
    from models.profile import UserProfile
    from pipeline.disqualifier import Disqualifier
    from pipeline.scorer import Scorer
    from scrapers.greenhouse import GreenhouseScraper
    from scrapers.akamai import AkamaiScraper
    from scrapers.allot import AllotScraper
    from scrapers.amazon import AmazonScraper
    from scrapers.apple import AppleScraper
    from scrapers.checkpoint import CheckPointScraper
    from scrapers.comeet import ComeetScraper
    from scrapers.custom import CustomScraper
    from scrapers.eightfold import EightfoldScraper
    from scrapers.elbit import ElbitScraper
    from scrapers.gem import GemScraper
    from scrapers.google import GoogleScraper
    from scrapers.ibm import IBMScraper
    from scrapers.lever import LeverScraper
    from scrapers.meta import MetaScraper
    from scrapers.mobileye import MobileyeScraper
    from scrapers.phenom import PhenomScraper
    from scrapers.paloalto import PaloAltoScraper
    from scrapers.successfactors import SuccessFactorsScraper
    from scrapers.radware import RadwareScraper
    from scrapers.smartrecruiters import SmartRecruitersScraper
    from scrapers.towersemi import TowerSemiScraper
    from scrapers.varonis import VaronisScraper
    from scrapers.workday import WorkdayScraper
    from llm import create_llm
    from pipeline.llm_filter import filter_jobs_with_llm
    # Load the user profile so both the Disqualifier (hard rules) and the Scorer
    # (soft scoring) share the same source of truth about the user's skills and
    # preferences. Path is configured in settings.yaml under agent.profile_path.
    profile_path = config.get("agent", {}).get(
        "profile_path", "tests/fixtures/user_profile.json"
    )

    llm_client = None

    # ── Resume parsing (optional) ────────────────────────────────────────────────
    if args.parse_resume:
        logger.info(f"Parsing resume: {args.parse_resume}")
        from resume.parser import parse_resume

        llm_client = create_llm(config.get("llm", {}))
        logger.info(f"Using LLM provider: {config.get('llm', {}).get('provider', 'claude')}")

        parsed_profile = parse_resume(args.parse_resume, llm_client, profile_path)
        logger.info(f"Profile written to {profile_path}")
        logger.info("Resume parsing complete. Run main.py again without --parse-resume to search jobs.")
        return

    profile = UserProfile.from_json(profile_path)

    llm_config = config.get("llm_filter", {})
    llm_client = llm_client or create_llm(config.get("llm", {}))
    batch_size = llm_config.get("batch_size", 5)

    use_llm_filter = args.llm_filter and llm_client

    # In LLM mode the disqualifier skips location checks — the LLM handles location
    # filtering instead, using geographic reasoning to handle cities not in the explicit
    # list (e.g. knowing a city is in the Tel Aviv area even if unlisted).
    disqualifier = Disqualifier(locations if not use_llm_filter else {}, profile,
                                config.get("disqualifier", {}).get("disqualify_remote", True))

    # In LLM mode the scorer uses relaxed thresholds (no required title keywords, lower
    # fuzzy-match cutoff) so the LLM sees a broad candidate pool and can apply its own
    # judgment. In strict mode the thresholds are tighter to compensate for no LLM step.
    strict_scorer = not args.llm_filter
    scorer = Scorer(profile, locations, tech_keywords, strict=strict_scorer,
                    scoring_config=config.get("scoring", {}))

    scrapers = {}

    # ATS platforms whose scraper accepts a keyword search query.
    # These receive job_titles from settings.yaml so the API pre-filters results
    # instead of fetching every job and discarding irrelevant ones afterwards.
    _SEARCH_SUPPORTED_ATS = {"eightfold", "phenom", "amazon", "google", "meta", "workday"}
    job_titles = config.get("search", {}).get("job_titles", [])

    def get_scraper(ats: str):
        """Get or create scraper for given ATS type."""
        scraper_classes = {
            "greenhouse": GreenhouseScraper,
            "lever": LeverScraper,
            "smartrecruiters": SmartRecruitersScraper,
            "workday": WorkdayScraper,
            "comeet": ComeetScraper,
            "mobileye": MobileyeScraper,
            "varonis": VaronisScraper,
            "eightfold": EightfoldScraper,
            "gem": GemScraper,
            "amazon": AmazonScraper,
            "google": GoogleScraper,
            "apple": AppleScraper,
            "ibm": IBMScraper,
            "meta": MetaScraper,
            "Allot": AllotScraper,
            "elbit": ElbitScraper,
            "phenom": PhenomScraper,
            "towersemi": TowerSemiScraper,
            "akamai": AkamaiScraper,
            "radware": RadwareScraper,
            "checkpoint": CheckPointScraper,
            "paloalto": PaloAltoScraper,
            "successfactors": SuccessFactorsScraper,
            "custom": CustomScraper,
        }
        if ats in scrapers:
            return scrapers[ats]
        elif ats in scraper_classes:
            delay = config["scraping"]["request_delay_seconds"]
            kwargs = {"request_delay": delay}
            if ats in _SEARCH_SUPPORTED_ATS and job_titles:
                kwargs["job_titles"] = job_titles
            scr = scraper_classes[ats](**kwargs)
        else:
            scr = None

        scrapers[ats] = scr
        return scr

    # ── Sheets init (skip in dry-run or test env) ──────────────────────────────
    sheets_writer  = None
    existing_urls: set[str] = set()

    # JOB_AGENT_ENV=test is set by conftest.py so tests never write to live Sheets
    is_test = os.environ.get("JOB_AGENT_ENV") == "test"

    if not args.dry_run and not is_test:
        from output.google_sheets import SheetsWriter
        sheets_writer = SheetsWriter(creds_path, spreadsheet_id, sheet_name)
        existing_urls = sheets_writer.load_existing_urls()
        logger.info(f"Loaded {len(existing_urls)} existing job URLs from Sheets")
    elif args.dry_run:
        logger.info("DRY RUN — no data will be written to Google Sheets")

    # ── Scrape, disqualify, score ──────────────────────────────────────────────
    slug_filter = set(args.companies) if args.companies else None
    all_qualified = []
    total_found = total_new = total_disqualified = total_below_threshold = 0

    # When using LLM filter, collect jobs above the looser pre-filter threshold
    # so the LLM sees a broader candidate pool (not just those above score_threshold).
    min_score_before_llm = llm_config.get("min_score_before_llm", 30)
    collection_threshold = min_score_before_llm if use_llm_filter else score_threshold

    for company_data in companies_cfg.get("companies", []):
        company = CompanyConfig(**company_data)

        if not company.enabled:
            continue

        current_scraper = get_scraper(company.ats)

        if current_scraper is None:
            logger.debug(f"  SKIP {company.name} ({company.ats} scraper not available)")
            continue

        if slug_filter and company.slug not in slug_filter and company.name.lower() not in slug_filter:
            continue

        logger.info(f"\n→ {company.name}  [ATS: {company.ats}]")

        try:
            jobs = current_scraper.fetch_jobs(company, max_age_days=max_age_days)
        except Exception as exc:
            logger.error(f"  ERROR scraping {company.name}: {exc}", exc_info=True)
            continue

        total_found += len(jobs)
        logger.info(f"  {len(jobs)} jobs within {max_age_days} days")

        # Dedup: skip jobs whose URL is already in the sheet from a previous run
        new_jobs = [j for j in jobs if j.url not in existing_urls]
        total_new += len(new_jobs)
        logger.info(f"  {len(new_jobs)} new (not already in sheet)")

        # Translate any Hebrew job postings to English before further processing.
        # Results are cached by URL so re-runs don't re-hit Google Translate.
        from pipeline.translator import translate_jobs
        translated_jobs = translate_jobs(new_jobs)

        # Hard disqualification: location, remote, excluded keywords/titles, seniority
        qualified_raw = []
        for job in translated_jobs:
            result = disqualifier.check(job)
            if result.is_disqualified:
                total_disqualified += 1
                logger.debug(f"  ✗ [DQ] {job.title} @ {job.location_raw} — {result.reason}")
            else:
                qualified_raw.append(job)
                logger.debug(f"  ✓ [DQ] {job.title} @ {job.location_raw}")

        # Score each qualified job and collect those above the effective threshold.
        # In LLM-filter mode we use the looser pre-filter threshold (e.g. 30) so the
        # LLM sees a broader candidate pool; in strict mode we use score_threshold (e.g. 60).
        qualified_scored = []
        for job in qualified_raw:
            scored = scorer.score(job)
            if scored.score < collection_threshold:
                total_below_threshold += 1
                logger.debug(
                    f"  ✗ [score {scored.score:.0f}<{collection_threshold}] "
                    f"{scored.title} @ {scored.location_raw}"
                )
            else:
                qualified_scored.append(scored)
                logger.debug(
                    f"  ✓ [score {scored.score:.0f}] {scored.title} @ {scored.location_raw}"
                )

        threshold_label = f"{collection_threshold} (pre-LLM)" if args.llm_filter else str(score_threshold)
        logger.info(f"  {len(qualified_scored)} above score threshold ({threshold_label})")
        all_qualified.extend(qualified_scored)

    # ── LLM filtering (optional) ────────────────────────────────────────────────
    if use_llm_filter and all_qualified:
        logger.info(f"\nApplying LLM filter to {len(all_qualified)} jobs...")
        logger.info(f"Using LLM provider: {config.get('llm', {}).get('provider', 'claude')}")
        logger.info(f"Batch size: {batch_size}, pre-filter threshold: {min_score_before_llm}")

        # filter_jobs_with_llm returns only the jobs the LLM classified as relevant.
        # Jobs in [min_score_before_llm, score_threshold) that the LLM approves are kept;
        # jobs the LLM rejects are excluded regardless of their numeric score.
        before_llm_count = len(all_qualified)
        all_qualified = filter_jobs_with_llm(
            all_qualified,
            profile,
            locations.get("main_areas", []),
            llm_client,
            batch_size=batch_size,
            min_score_before_llm=0,
        )
        logger.info(f"LLM rejected {before_llm_count - len(all_qualified)} jobs, {len(all_qualified)} remaining")

    # ── Cover letter generation (optional) ─────────────────────────────────────
    if args.generate_cover_letters and all_qualified:
        logger.info(f"\nGenerating cover letters for {len(all_qualified)} qualified jobs...")
        from pipeline.cover_letter import generate_cover_letters

        llm_client = llm_client or create_llm(config.get("llm", {}))
        logger.info(f"Using LLM provider: {config.get('llm', {}).get('provider', 'claude')}")

        all_qualified = generate_cover_letters(all_qualified, profile, llm_client, score_threshold)
        logger.info("Cover letters generated.")

    # ── Summary ────────────────────────────────────────────────────────────────
    logger.info(f"\n{'─' * 50}")
    logger.info(f"Found:           {total_found}")
    logger.info(f"New:             {total_new}")
    logger.info(f"Disqualified:    {total_disqualified}")
    logger.info(f"Below threshold: {total_below_threshold}")
    logger.info(f"Qualified:       {len(all_qualified)}")

    if args.generate_cover_letters:
        logger.info(f"Cover letters:   {sum(1 for j in all_qualified if j.cover_letter)}")

    if not all_qualified:
        logger.info("No new qualifying jobs.")
        return

    if args.dry_run or is_test:
        logger.info(f"\nJobs that would be written to Sheets (score ≥ {score_threshold}):")
        for job in sorted(all_qualified, key=lambda j: j.score, reverse=True):
            bd = job.score_breakdown
            cl_indicator = " [CL]" if job.cover_letter else ""
            logger.info(
                f"  [{job.score:.0f}]{cl_indicator} {job.company}: {job.title}"
                f" | {job.location_raw}"
                f" | title={bd.get('title', 0):.0f}"
                f" tech={bd.get('tech_stack', 0):.0f}"
                f" loc={bd.get('location', 0):.0f}"
            )
    else:
        sheets_writer.append_jobs(all_qualified)
        logger.info(f"\n✓ Written {len(all_qualified)} jobs to Google Sheets")


if __name__ == "__main__":
    main()
