"""Tests for pipeline/translator.py"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from models.job import RawJob
from pipeline.translator import Translator, translate_jobs


@pytest.fixture
def translator():
    return Translator(cache_dir=".cache/test_translations")


@pytest.fixture
def job_hebrew():
    return RawJob(
        id="test-1",
        title="מהנדס למידה",
        company="TestCo",
        url="http://test.com/job1",
        location_raw="תל אביב",
        description_html="<p>אנחנו מחפשים מהנדס למידה למכונה</p>",
        source="greenhouse",
    )


@pytest.fixture
def job_english():
    return RawJob(
        id="test-2",
        title="Data Scientist",
        company="TestCo",
        url="http://test.com/job2",
        location_raw="Tel Aviv",
        description_html="<p>We are looking for a data scientist</p>",
        source="greenhouse",
    )


class TestTranslator:
    def test_detect_hebrew(self, translator, job_hebrew):
        text = f"{job_hebrew.title} {job_hebrew.description_html}"
        lang = translator.detect_language(text)
        assert lang == "he"

    def test_detect_english(self, translator, job_english):
        text = f"{job_english.title} {job_english.description_html}"
        lang = translator.detect_language(text)
        assert lang is None

    def test_detect_empty_text(self, translator):
        lang = translator.detect_language("")
        assert lang is None

    def test_translate_job_no_english(self, translator, job_english):
        result = translator.translate_job(job_english)
        assert result.detected_language is None
        assert result.translated is False

    def test_cache_load(self, translator, job_hebrew):
        with patch.object(translator, '_load_from_cache', return_value={"title": "Translated"}):
            result = translator.translate_job(job_hebrew)
            assert result.title == "Translated"
            assert result.translated is True


class TestTranslateJobs:
    def test_translate_list(self, translator, job_hebrew, job_english):
        with patch.object(translator, 'translate_job', side_effect=[job_hebrew, job_english]):
            jobs = translate_jobs([job_hebrew, job_english], cache_dir=".cache/test")
            assert len(jobs) == 2