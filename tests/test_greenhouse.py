"""Unit tests for scrapers/greenhouse.py"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import responses as resp_mock

from models.job import CompanyConfig
from scrapers.greenhouse import GreenhouseScraper, _API_BASE

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_raw_response.json"


@pytest.fixture
def company():
    return CompanyConfig(name="TestCo", ats="greenhouse", slug="testco", enabled=True)


@pytest.fixture
def fixture_data():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


@pytest.fixture
def scraper():
    return GreenhouseScraper(request_delay=0)


def _mock_api(slug: str, payload: dict) -> None:
    resp_mock.add(
        resp_mock.GET,
        f"{_API_BASE}/{slug}/jobs",
        json=payload,
        status=200,
    )


# ── Field normalization ────────────────────────────────────────────────────────

@pytest.mark.unit
@resp_mock.activate
def test_normalizes_required_fields(scraper, company, fixture_data):
    _mock_api(company.slug, fixture_data)
    jobs = scraper.fetch_jobs(company, max_age_days=60)
    assert len(jobs) > 0
    job = jobs[0]
    assert job.source == "greenhouse"
    assert job.company == company.name
    assert job.id.startswith("greenhouse:")
    assert job.url.startswith("https://")


@pytest.mark.unit
@resp_mock.activate
def test_id_is_prefixed_with_source(scraper, company, fixture_data):
    _mock_api(company.slug, fixture_data)
    jobs = scraper.fetch_jobs(company, max_age_days=60)
    for job in jobs:
        assert job.id.startswith("greenhouse:")


@pytest.mark.unit
@resp_mock.activate
def test_location_extracted_from_location_name_field(scraper, company, fixture_data):
    _mock_api(company.slug, fixture_data)
    jobs = scraper.fetch_jobs(company, max_age_days=60)
    # First fixture job is in "Tel Aviv, Israel"
    tel_aviv_job = next(j for j in jobs if j.title == "Data Scientist")
    assert tel_aviv_job.location_raw == "Tel Aviv, Israel"


@pytest.mark.unit
@resp_mock.activate
def test_description_html_extracted(scraper, company, fixture_data):
    _mock_api(company.slug, fixture_data)
    jobs = scraper.fetch_jobs(company, max_age_days=60)
    job = next(j for j in jobs if j.title == "Data Scientist")
    assert "Python" in job.description_html
    assert "<" in job.description_html  # still HTML, not stripped


# ── Age filtering ──────────────────────────────────────────────────────────────

@pytest.mark.unit
@resp_mock.activate
def test_filters_jobs_older_than_max_age(scraper, company):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Fresh Job",
                "location": {"name": "Tel Aviv, Israel"},
                "updated_at": fresh,
                "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                "content": "<p>Fresh</p>",
            },
            {
                "id": 2,
                "title": "Old Job",
                "location": {"name": "Tel Aviv, Israel"},
                "updated_at": old,
                "absolute_url": "https://boards.greenhouse.io/x/jobs/2",
                "content": "<p>Old</p>",
            },
        ],
        "meta": {"total": 2},
    }
    _mock_api(company.slug, payload)
    jobs = scraper.fetch_jobs(company, max_age_days=30)
    assert len(jobs) == 1
    assert jobs[0].title == "Fresh Job"


@pytest.mark.unit
@resp_mock.activate
def test_job_one_day_before_cutoff_is_included(scraper, company):
    """Job posted one day inside the window should be included."""
    now = datetime.now(timezone.utc)
    exactly_30 = (now - timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "jobs": [
            {
                "id": 99,
                "title": "Boundary Job",
                "location": {"name": "Tel Aviv, Israel"},
                "updated_at": exactly_30,
                "absolute_url": "https://boards.greenhouse.io/x/jobs/99",
                "content": "<p>Boundary</p>",
            }
        ],
        "meta": {"total": 1},
    }
    _mock_api(company.slug, payload)
    jobs = scraper.fetch_jobs(company, max_age_days=30)
    assert len(jobs) == 1


@pytest.mark.unit
@resp_mock.activate
def test_fixture_has_expected_age_filtered_result(scraper, company, fixture_data):
    """The fixture has one job posted 50 days ago — it should be filtered at max_age_days=30."""
    _mock_api(company.slug, fixture_data)
    jobs_30 = scraper.fetch_jobs(company, max_age_days=30)
    jobs_60 = scraper.fetch_jobs(company, max_age_days=60)
    assert len(jobs_60) > len(jobs_30)


# ── Error handling ─────────────────────────────────────────────────────────────

@pytest.mark.unit
@resp_mock.activate
def test_returns_empty_list_when_api_returns_no_jobs(scraper, company):
    _mock_api(company.slug, {"jobs": [], "meta": {"total": 0}})
    jobs = scraper.fetch_jobs(company, max_age_days=30)
    assert jobs == []


@pytest.mark.unit
@resp_mock.activate
def test_raises_when_api_returns_4xx(scraper, company):
    resp_mock.add(
        resp_mock.GET,
        f"{_API_BASE}/{company.slug}/jobs",
        json={"error": "not found"},
        status=404,
    )
    with pytest.raises(Exception):
        scraper.fetch_jobs(company, max_age_days=30)


@pytest.mark.unit
def test_raises_when_slug_is_missing(scraper):
    company = CompanyConfig(name="No Slug Co", ats="greenhouse", slug=None, enabled=True)
    with pytest.raises(ValueError, match="slug"):
        scraper.fetch_jobs(company)


@pytest.mark.unit
@resp_mock.activate
def test_skips_malformed_job_without_crashing(scraper, company):
    """A job missing required fields should be skipped, not crash the scraper."""
    payload = {
        "jobs": [
            {"id": 1, "title": "Good Job", "location": {"name": "Tel Aviv, Israel"},
             "updated_at": "2026-04-19T10:00:00.000Z",
             "absolute_url": "https://boards.greenhouse.io/x/jobs/1", "content": "<p>ok</p>"},
            {"id": 2},  # missing almost everything
        ],
        "meta": {"total": 2},
    }
    _mock_api(company.slug, payload)
    jobs = scraper.fetch_jobs(company, max_age_days=30)
    assert len(jobs) == 1
    assert jobs[0].title == "Good Job"


# ── URL normalization (dedup) ──────────────────────────────────────────────────

@pytest.mark.unit
@resp_mock.activate
def test_tracking_params_stripped_from_url(scraper, company):
    payload = {
        "jobs": [
            {
                "id": 10,
                "title": "Clean URL Job",
                "location": {"name": "Tel Aviv, Israel"},
                "updated_at": "2026-04-19T10:00:00.000Z",
                "absolute_url": "https://boards.greenhouse.io/x/jobs/10?gh_src=abc123&utm_source=linkedin",
                "content": "<p>ok</p>",
            }
        ],
        "meta": {"total": 1},
    }
    _mock_api(company.slug, payload)
    jobs = scraper.fetch_jobs(company, max_age_days=30)
    assert "gh_src" not in jobs[0].url
    assert "utm_source" not in jobs[0].url


# ── Integration ────────────────────────────────────────────────────────────────

@pytest.mark.integration
@resp_mock.activate
def test_full_fixture_all_jobs_pass_pydantic_validation(scraper, company, fixture_data):
    """All jobs in the fixture should normalize without Pydantic errors."""
    _mock_api(company.slug, fixture_data)
    jobs = scraper.fetch_jobs(company, max_age_days=60)
    assert len(jobs) >= 5
    for job in jobs:
        assert job.id
        assert job.url
        assert job.company == "TestCo"
