"""
Logging configuration for Job Search Agent.

Two log files are maintained:
  logs/job_searcher.log  — all messages DEBUG and above (full history)
  logs/errors.log        — WARNING and above only (quick error triage)

Console output uses a plain format (message only) so it looks like normal
program output. File output includes timestamps and module names.

When the --verbose flag is active, main.py lowers the console handler level
to DEBUG so verbose job-level detail appears on screen AND in the log file.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


def setup_logging(
    log_dir: str = "logs",
    log_level: str = "DEBUG",
    console_level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> logging.Logger:
    """
    Configure logging with file rotation and separate console/file handlers.

    Parameters
    ----------
    log_dir       : directory for log files (created if missing)
    log_level     : minimum level captured by file handlers (default DEBUG)
    console_level : minimum level printed to stdout (default INFO;
                    pass "DEBUG" when --verbose is active)
    max_bytes     : max size per log file before rotation
    backup_count  : number of rotated backup files to keep
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("job_searcher")
    logger.setLevel(logging.DEBUG)  # let handlers decide their own floor
    logger.handlers.clear()

    # Console: plain message-only format so output looks like normal print()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    # Main log file: full detail with timestamps and logger name
    file_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path / "job_searcher.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Errors-only log: WARNING+ for quick incident triage
    error_handler = RotatingFileHandler(
        log_path / "errors.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(file_formatter)
    logger.addHandler(error_handler)

    return logger


def get_logger(name: str = "job_searcher") -> logging.Logger:
    """Get a child logger that inherits the configured handlers."""
    return logging.getLogger(name)