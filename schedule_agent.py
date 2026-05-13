#!/usr/bin/env python3
"""
Job Search Agent Scheduler.

Runs the job search agent on a schedule using APScheduler.
Useful for automated daily job searches (e.g., every weekday morning).

Usage:
    python schedule_agent.py              # Run with default config (disabled)
    python schedule_agent.py --enable     # Enable and run scheduler
    python schedule_agent.py --run-once   # Run once immediately
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


def run_job_search() -> None:
    """Run the main job search script."""
    import main as job_search_main
    sys.argv = ["main.py", "--dry-run"]
    job_search_main.main()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Job Search Agent Scheduler")
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable the scheduler (otherwise runs once with --run-once)",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run job search immediately once and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (don't write to Sheets)",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    schedule_config = config.get("schedule", {})
    enabled = args.enable or schedule_config.get("enabled", False)
    cron_expr = schedule_config.get("cron", "0 8 * * 1-5")

    if args.run_once:
        print("Running job search once...")
        if args.dry_run:
            sys.argv = ["main.py", "--dry-run"]
        else:
            sys.argv = ["main.py"]
        try:
            import main as job_search_main
            job_search_main.main()
        except Exception as e:
            print(f"Error during job search: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if not enabled:
        print("Scheduler is disabled. Use --enable to activate or --run-once to run once.")
        print(f"Current config: enabled={schedule_config.get('enabled', False)}, cron={cron_expr}")
        print("To enable, add to config/settings.yaml:")
        print("  schedule:")
        print("    enabled: true")
        print("    cron: '0 8 * * 1-5'  # 08:00 Monday-Friday")
        return

    print(f"Starting scheduler with cron: {cron_expr}")

    scheduler = BlockingScheduler()

    parts = cron_expr.split()
    if len(parts) != 5:
        print(f"Invalid cron expression: {cron_expr}", file=sys.stderr)
        sys.exit(1)

    minute, hour, day, month, day_of_week = parts

    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=month if month != "*" else None,
        day_of_week=day_of_week,
    )

    job_args = ["main.py"]
    if args.dry_run:
        job_args.append("--dry-run")

    scheduler.add_job(run_job_search, trigger, args=job_args)

    print(f"Scheduler started. Press Ctrl+C to stop.")
    print(f"Next run: {scheduler.get_jobs()[0].next_run_time}")

    try:
        scheduler.start()
    except (KeyboardInterrupt, InterruptedError):
        print("\nScheduler stopped.")


if __name__ == "__main__":
    main()