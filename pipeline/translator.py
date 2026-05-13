"""
Hebrew job detection and translation.

Detects if a job posting is in Hebrew and translates it to English using
Google Translate (via deep-translator). Caches translations by job URL
to avoid re-translating on every run.

Usage:
    from pipeline.translator import Translator
    translator = Translator(cache_dir=".cache/translations")
    job = translator.translate_if_needed(job)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from deep_translator import GoogleTranslator

from models.job import RawJob

_log = logging.getLogger("job_searcher.translator")


class Translator:
    """Language detector and translator for job postings."""

    _HEBREW_PATTERN = re.compile(r'[\u0590-\u05FF]')

    def __init__(self, cache_dir: str = ".cache/translations"):
        """
        Initialize the translator.

        Args:
            cache_dir: Directory to store translation cache.
        """
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._translator = GoogleTranslator(source="auto", target="en")

    def _get_cache_path(self, url: str) -> Path:
        """Get cache file path for a job URL."""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return self._cache_dir / f"{url_hash}.json"

    def _load_from_cache(self, url: str) -> Optional[dict]:
        """Load translation from cache."""
        cache_path = self._get_cache_path(url)
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return None

    def _save_to_cache(self, url: str, data: dict) -> None:
        """Save translation to cache."""
        cache_path = self._get_cache_path(url)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except IOError:
            pass

    def detect_language(self, text: str) -> Optional[str]:
        """
        Detect the language of text.

        Args:
            text: Text to detect language for.

        Returns:
            Language code (e.g., 'he' for Hebrew, 'en' for English) or None.
        """
        if not text or len(text.strip()) < 10:
            return None

        if self._HEBREW_PATTERN.search(text):
            return "he"

        return None

    def translate(self, text: str) -> str:
        """
        Translate text to English.

        Args:
            text: Text to translate.

        Returns:
            Translated text.
        """
        if not text:
            return ""

        try:
            translated = self._translator.translate(text)
            return translated if translated else text
        except Exception as e:
            _log.warning(f"Translation failed: {e}")
            return text

    def translate_job(self, job: RawJob) -> RawJob:
        """
        Translate a job posting if it's in Hebrew.

        Args:
            job: RawJob to translate.

        Returns:
            RawJob with translated content (original preserved in raw_payload).
        """
        if job.translated:
            return job

        cached = self._load_from_cache(job.url)
        if cached:
            job.description_html = cached.get("description_html", job.description_html)
            job.title = cached.get("title", job.title)
            job.detected_language = cached.get("detected_language", "he")
            job.translated = True
            return job

        text_to_check = f"{job.title} "
        if job.description_html:
            from bs4 import BeautifulSoup
            text_to_check += BeautifulSoup(job.description_html).get_text()

        detected = self.detect_language(text_to_check)

        if detected == "he":
            job.detected_language = "he"

            job.title = self.translate(job.title)

            if job.description_html:
                from bs4 import BeautifulSoup
                desc_text = BeautifulSoup(job.description_html).get_text()
                translated_desc = self.translate(desc_text)
                job.description_html = f"<p>{translated_desc}</p>"

            self._save_to_cache(job.url, {
                "title": job.title,
                "description_html": job.description_html,
                "detected_language": "he",
            })

            job.translated = True
            return job

        return job


def translate_jobs(jobs: list[RawJob], cache_dir: str = ".cache/translations") -> list[RawJob]:
    """
    Translate all Hebrew job postings to English.

    Args:
        jobs: List of RawJob to translate.
        cache_dir: Directory for translation cache.

    Returns:
        List of RawJob with Hebrew jobs translated.
    """
    _log.info(f"Translating {len(jobs)} jobs (Language detection + caching)...")
    translator = Translator(cache_dir=cache_dir)
    return [translator.translate_job(job) for job in jobs]