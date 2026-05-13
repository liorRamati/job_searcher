"""Unit and integration tests for pipeline/scorer.py"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from models.job import RawJob
from models.profile import HardDisqualifiers, UserProfile, UserSkills
from pipeline.scorer import (
    Scorer,
    _W_LOCATION,
    _W_REQUIREMENTS,
    _W_TECH,
    _W_TITLE,
    _W_WORK_TYPE,
)

FIXTURE_PATH   = Path(__file__).parent / "fixtures" / "greenhouse_raw_response.json"
LOCATIONS_PATH = "config/locations.yaml"
TECH_KW_PATH   = "config/tech_keywords.yaml"


# ── Shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def locations():
    with open(LOCATIONS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def tech_keywords():
    with open(TECH_KW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@pytest.fixture
def profile():
    """Minimal but realistic profile — mirrors Lior's actual user_profile.json."""
    return UserProfile(
        name="Test User",
        seniority="mid",
        years_of_experience=3,
        target_titles=[
            "data scientist",
            "algorithm developer",
            "ml engineer",
            "research engineer",
        ],
        skills=UserSkills(
            languages=["Python", "SQL"],
            frameworks=["PyTorch", "Pandas", "NumPy", "scikit-learn"],
            tools=["Git"],
            domains=["machine learning", "data science"],
        ),
        hard_disqualifiers=HardDisqualifiers(
            excluded_keywords=["C++", "FPGA", "PHP"],
            min_years_required_max=7,
            excluded_titles=["Manager", "Director", "VP"],
        ),
    )


@pytest.fixture
def scorer(profile, locations, tech_keywords):
    return Scorer(profile, locations, tech_keywords)


def job(**kwargs) -> RawJob:
    """
    Factory: creates a RawJob with sensible defaults that represent a good match
    (Data Scientist in Tel Aviv with Python/PyTorch). Individual tests override
    only the fields relevant to what they're testing.
    """
    defaults = dict(
        id="test-1",
        title="Data Scientist",
        company="TestCo",
        url="https://example.com/job/1",
        location_raw="Tel Aviv, Israel",
        description_html=(
            "<p>We are looking for a Data Scientist. "
            "Requires Python and PyTorch. "
            "3 years of experience required. "
            "Hybrid work arrangement.</p>"
        ),
        source="greenhouse",
        posted_date=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return RawJob(**defaults)


# ── Title scoring ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_title_exact_match_earns_full_points(scorer):
    """An exact target title string should score the maximum 25 title points."""
    pts, _ = scorer._score_title("data scientist")
    assert pts == pytest.approx(_W_TITLE, abs=1)


@pytest.mark.unit
def test_title_partial_match_scores_high(scorer):
    """
    'Senior Data Scientist' should still score strongly against 'data scientist'.
    token_set_ratio handles extra words gracefully — it sees 'data scientist' as
    a perfect subset of 'senior data scientist'.
    """
    pts, _ = scorer._score_title("Senior Data Scientist")
    assert pts >= 20


@pytest.mark.unit
def test_title_unrelated_scores_low(scorer):
    """'Product Manager' has minimal overlap with any target title."""
    pts, _ = scorer._score_title("Product Manager")
    # rapidfuzz gives a non-zero ratio due to character-level similarity,
    # so we allow up to 13 pts (ratio ~52/100 * 25) — still well below a real match
    assert pts <= 13


@pytest.mark.unit
def test_title_breakdown_contains_best_match_and_ratio(scorer):
    """Breakdown dict must include both the winning target title and the raw ratio."""
    _, info = scorer._score_title("ML Engineer")
    assert "title_best_match" in info
    assert "title_ratio" in info
    # The best match should be one of the profile's target titles
    assert info["title_best_match"] in [
        "data scientist", "algorithm developer", "ml engineer", "research engineer"
    ]


@pytest.mark.unit
def test_title_case_insensitive(scorer):
    """Scoring should be identical regardless of capitalisation."""
    pts_lower, _ = scorer._score_title("data scientist")
    pts_upper, _ = scorer._score_title("DATA SCIENTIST")
    assert pts_lower == pytest.approx(pts_upper, abs=0.1)


# ── Tech stack scoring ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_tech_many_matches_earns_full_points(scorer):
    """Mentioning >= FULL_MATCH_COUNT skills should hit the 30-point cap."""
    html = "<p>Python, PyTorch, SQL, Pandas, NumPy, Git, machine learning, data science</p>"
    pts, info = scorer._score_tech(html)
    assert pts == pytest.approx(_W_TECH, abs=0.1)


@pytest.mark.unit
def test_tech_no_matches_earns_zero(scorer):
    """A completely unrelated tech stack should score exactly 0."""
    html = "<p>We need Ruby on Rails and PHP experience with React.js.</p>"
    pts, info = scorer._score_tech(html)
    assert pts == 0
    assert info["matched"] == []


@pytest.mark.unit
def test_tech_alias_matching_pytorch(scorer):
    """'torch' in the description should match the 'PyTorch' skill via alias."""
    html = "<p>Experience with torch and sklearn required.</p>"
    _, info = scorer._score_tech(html)
    assert "PyTorch" in info["matched"]


@pytest.mark.unit
def test_tech_alias_matching_sklearn(scorer):
    """'sklearn' should match the 'scikit-learn' skill."""
    html = "<p>Build models using sklearn and pandas.</p>"
    _, info = scorer._score_tech(html)
    assert "scikit-learn" in info["matched"]


@pytest.mark.unit
def test_tech_matched_list_in_breakdown(scorer):
    """Matched skill names should appear in the info dict under 'matched'."""
    html = "<p>Python and SQL experience required.</p>"
    _, info = scorer._score_tech(html)
    assert "Python" in info["matched"]
    assert "SQL" in info["matched"]


@pytest.mark.unit
def test_tech_partial_match_is_proportional(scorer):
    """Matching 3 of 6 required skills earns half of 30 pts (15 pts)."""
    # Python + PyTorch + machine learning = 3 matches
    html = "<p>Python, PyTorch, and machine learning background needed.</p>"
    pts, info = scorer._score_tech(html)
    # 3/6 * 30 = 15
    assert pts == pytest.approx(15, abs=1)


# ── Location scoring ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_location_tier1_earns_max_points(scorer):
    """Tel Aviv is tier 1 — should score the maximum 20 location points."""
    pts, info = scorer._score_location("Tel Aviv, Israel")
    assert pts == pytest.approx(20, abs=0.1)
    assert info["tier"] == 1


@pytest.mark.unit
def test_location_tier2_without_train_scores_base(scorer):
    """Shoham (tier 2, no train station) should earn 15 pts — less than tier 1."""
    pts, info = scorer._score_location("Shoham, Israel")
    assert pts == pytest.approx(15, abs=0.1)
    assert info["tier"] == 2
    assert info["train_bonus"] == 0


@pytest.mark.unit
def test_location_tier2_with_train_bonus(scorer):
    """
    Modi'in is tier 2 AND has a train station.
    Score = 15 (tier base) + 3 (train bonus) = 18 pts.
    """
    pts, info = scorer._score_location("Modi'in, Israel")
    assert pts == pytest.approx(18, abs=0.1)
    assert info["tier"] == 2
    assert info["train_bonus"] == 3


@pytest.mark.unit
def test_location_tier3_jerusalem(scorer):
    """Jerusalem is tier 3 (12 pts base) + train bonus (3) = 15 pts."""
    pts, info = scorer._score_location("Jerusalem, Israel")
    assert pts == pytest.approx(15, abs=0.1)
    assert info["tier"] == 3


@pytest.mark.unit
def test_location_ambiguous_israel_gets_neutral_score(scorer):
    """'Israel' alone is too vague — give benefit of the doubt with neutral score."""
    pts, info = scorer._score_location("Israel")
    assert pts == pytest.approx(10, abs=0.1)
    assert info["area"] == "ambiguous"


@pytest.mark.unit
def test_location_none_gets_neutral_score(scorer):
    """Missing location -> same neutral score as 'ambiguous' Israel."""
    pts, info = scorer._score_location(None)
    assert pts == pytest.approx(10, abs=0.1)
    assert info["area"] == "unknown"


@pytest.mark.unit
def test_location_train_bonus_not_applied_to_non_train_city(scorer):
    """Ramat Gan is tier 1 but has no direct train station — no bonus."""
    pts, info = scorer._score_location("Ramat Gan, Israel")
    assert info["train_bonus"] == 0
    # Still scores full 20 pts because it's tier 1
    assert pts == pytest.approx(20, abs=0.1)


@pytest.mark.unit
def test_location_herzliya_tier1_with_train_is_capped(scorer):
    """
    Herzliya is tier 1 (20 pts base) and has a train station (+3).
    The bonus is present in the info dict but the score is capped at 20.
    """
    pts, info = scorer._score_location("Herzliya, Israel")
    assert pts == pytest.approx(20, abs=0.1)   # capped at max
    assert info["train_bonus"] == 3             # bonus was computed but capped


@pytest.mark.unit
def test_location_hebrew_city_name(scorer):
    """Hebrew 'הרצליה' (Herzliya) should translate and score as tier 1."""
    pts, info = scorer._score_location("הרצליה")
    assert pts == pytest.approx(20, abs=0.1)
    assert info["tier"] == 1


# ── Work type scoring ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_work_type_hybrid_earns_max_points(scorer):
    """'hybrid' in the description -> user's preferred arrangement -> 10 pts."""
    j = job(
        location_raw="Tel Aviv",
        description_html="<p>Hybrid model: 3 days in office, 2 remote.</p>",
    )
    pts, info = scorer._score_work_type(j)
    assert pts == _W_WORK_TYPE
    assert info["work_arrangement"] == "hybrid"


@pytest.mark.unit
def test_work_type_hybrid_detected_in_location_field(scorer):
    """Hybrid signal in the location field (not description) should still score 10."""
    j = job(location_raw="Hybrid - Tel Aviv", description_html="<p>Join us.</p>")
    pts, info = scorer._score_work_type(j)
    assert pts == _W_WORK_TYPE
    assert info["work_arrangement"] == "hybrid"


@pytest.mark.unit
def test_work_type_on_site_scores_less_than_hybrid(scorer):
    """On-site is acceptable but not preferred -> 8 pts."""
    j = job(
        location_raw="Tel Aviv",
        description_html="<p>This is an on-site position at our Tel Aviv office.</p>",
    )
    pts, info = scorer._score_work_type(j)
    assert pts == 8
    assert info["work_arrangement"] == "on-site"


@pytest.mark.unit
def test_work_type_unknown_earns_neutral_score(scorer):
    """No work-type signal -> neutral score (better than zero, since we don't know)."""
    j = job(description_html="<p>Join our data science team and solve hard problems.</p>")
    pts, info = scorer._score_work_type(j)
    assert pts == 6
    assert info["work_arrangement"] == "unknown"


@pytest.mark.unit
def test_work_type_hybrid_takes_priority_over_onsite(scorer):
    """
    If both 'hybrid' and 'on-site' appear (e.g. 'hybrid on-site schedule'),
    hybrid is checked first and wins.
    """
    j = job(description_html="<p>Hybrid arrangement — on-site 3 days per week.</p>")
    pts, info = scorer._score_work_type(j)
    assert info["work_arrangement"] == "hybrid"
    assert pts == _W_WORK_TYPE


# ── Requirements scoring ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_requirements_exact_match_earns_full_points(scorer):
    """Required years == user's years -> 15 pts."""
    pts, info = scorer._score_requirements("<p>3 years of experience required.</p>")
    assert pts == _W_REQUIREMENTS
    assert info["years_required"] == 3


@pytest.mark.unit
def test_requirements_user_over_qualified(scorer):
    """Required years < user's years -> still full 15 pts (gap <= 0)."""
    pts, _ = scorer._score_requirements("<p>2 years of experience required.</p>")
    assert pts == _W_REQUIREMENTS


@pytest.mark.unit
def test_requirements_one_year_short(scorer):
    """Required 4 years, user has 3 -> gap of 1 -> 12 pts."""
    pts, info = scorer._score_requirements("<p>4 years of experience required.</p>")
    assert pts == 12
    assert info["years_required"] == 4


@pytest.mark.unit
def test_requirements_two_years_short(scorer):
    """Required 5 years, user has 3 -> gap of 2 -> 8 pts."""
    pts, _ = scorer._score_requirements("<p>5 years of experience required.</p>")
    assert pts == 8


@pytest.mark.unit
def test_requirements_far_short_earns_minimum(scorer):
    """
    Required 7 years, user has 3 -> gap of 4 -> minimum 3 pts.
    7 years is at the hard-disqualifier threshold; this tests the scorer's handling
    when the disqualifier chose not to block (it's exactly at the limit, not over it).
    """
    pts, _ = scorer._score_requirements("<p>7 years of experience required.</p>")
    assert pts == 3


@pytest.mark.unit
def test_requirements_no_mention_earns_neutral(scorer):
    """No years requirement stated -> neutral 10 pts."""
    pts, info = scorer._score_requirements("<p>Python and SQL skills required.</p>")
    assert pts == 10
    assert info["years_required"] is None


@pytest.mark.unit
def test_requirements_range_uses_lower_bound(scorer):
    """
    '3-5 years of experience' — regex captures the first number (3), which equals
    the user's years -> gap = 0 -> 15 pts.
    """
    pts, info = scorer._score_requirements("<p>3-5 years of experience required.</p>")
    assert pts == _W_REQUIREMENTS


@pytest.mark.unit
def test_requirements_ignores_calendar_years(scorer):
    """
    Numbers like '2024' should not be treated as years-of-experience requirements.
    The filter 1 <= y <= 30 in _score_requirements rejects them.
    """
    pts, info = scorer._score_requirements(
        "<p>Established in 2024. Strong Python skills needed.</p>"
    )
    assert info["years_required"] is None
    assert pts == 10  # neutral


# ── Full score integration ─────────────────────────────────────────────────────

@pytest.mark.integration
def test_score_is_between_0_and_100(scorer):
    """Every valid job must produce a total score in [0, 100]."""
    test_jobs = [
        job(),  # default — near-ideal
        job(title="Product Manager", location_raw="Tel Aviv"),
        job(title="Data Scientist", location_raw="Modi'in"),
        job(title="Algorithm Developer", description_html="<p>C++ and MATLAB experience.</p>"),
        job(location_raw=None),
        job(location_raw="Israel"),
    ]
    for j in test_jobs:
        result = scorer.score(j)
        assert 0 <= result.score <= 100, (
            f"Score out of range for '{j.title}' @ '{j.location_raw}': {result.score}"
        )


@pytest.mark.integration
def test_score_breakdown_components_sum_to_total(scorer):
    """
    The five numeric breakdown keys must sum exactly to the total score.
    This guards against copy-paste errors in the score() method.
    Note: score_breakdown also contains non-numeric detail keys like
    'title_best_match' and 'work_arrangement' — only the five dimension keys are summed.
    """
    result = scorer.score(job())
    bd = result.score_breakdown
    component_sum = (
        bd["title"]
        + bd["tech_stack"]
        + bd["location"]
        + bd["work_type"]       # numeric entry — distinct from "work_arrangement" (string)
        + bd["requirements"]
    )
    assert component_sum == pytest.approx(result.score, abs=0.1)


@pytest.mark.integration
def test_ideal_job_scores_above_qualifying_threshold(scorer):
    """
    A near-perfect match (correct title, matching tech, Tel Aviv, hybrid, right seniority)
    should score well above the 60-pt qualifying threshold.
    """
    ideal = job(
        title="Data Scientist",
        location_raw="Tel Aviv, Israel",
        description_html=(
            "<p>Data Scientist role. Python, PyTorch, Pandas, SQL, NumPy, "
            "machine learning, data science. "
            "3 years of experience required. Hybrid work model.</p>"
        ),
    )
    result = scorer.score(ideal)
    assert result.score >= 75, f"Expected >=75 for ideal job, got {result.score}"


@pytest.mark.integration
def test_product_manager_scores_below_qualifying_threshold(scorer):
    """
    A PM job should score well below the 60-pt threshold so it's not written to Sheets.
    Title similarity is near-zero; there's no tech overlap either.
    The job still gets full location points (Tel Aviv) + neutral work/requirements,
    landing at ~46-47 pts — comfortably below the 60-pt cutoff.
    """
    pm_job = job(
        title="Product Manager",
        description_html="<p>Lead product strategy for our B2B SaaS platform.</p>",
    )
    result = scorer.score(pm_job)
    assert result.score <= 50, f"Expected <=50 for PM job, got {result.score}"


@pytest.mark.integration
def test_scored_job_work_type_field_matches_breakdown(scorer):
    """The top-level work_type field on ScoredJob should match score_breakdown work_arrangement."""
    hybrid_job = job(description_html="<p>Hybrid work arrangement. Python required.</p>")
    result = scorer.score(hybrid_job)
    assert result.work_type == "hybrid"
    # The string label lives under "work_arrangement" in the breakdown
    assert result.score_breakdown["work_arrangement"] == result.work_type


@pytest.mark.integration
def test_scored_job_tech_stack_found_is_populated(scorer):
    """tech_stack_found should list matched skill names from the description."""
    result = scorer.score(job())  # default description has Python and PyTorch
    assert len(result.tech_stack_found) > 0
    assert "Python" in result.tech_stack_found


@pytest.mark.integration
def test_higher_tier_location_produces_higher_score(scorer):
    """
    Same job, two locations: Tel Aviv (tier 1) must score higher than Shoham (tier 2).
    This validates that the tier system flows through to the final score.
    """
    tel_aviv_score = scorer.score(job(location_raw="Tel Aviv, Israel")).score
    shoham_score   = scorer.score(job(location_raw="Shoham, Israel")).score  # tier 2, no train
    assert tel_aviv_score > shoham_score


@pytest.mark.integration
def test_hybrid_job_scores_higher_than_onsite_job(scorer):
    """
    Identical jobs except one is hybrid and one is on-site.
    The hybrid job should score exactly 2 pts higher (10 vs 8 work-type pts).
    """
    base_desc = "<p>Python and PyTorch. 3 years of experience. {arrangement}</p>"
    hybrid_score = scorer.score(job(
        description_html=base_desc.format(arrangement="Hybrid work model.")
    )).score
    onsite_score = scorer.score(job(
        description_html=base_desc.format(arrangement="On-site position.")
    )).score
    assert hybrid_score > onsite_score
    assert hybrid_score - onsite_score == pytest.approx(2, abs=0.1)


@pytest.mark.integration
def test_fixture_high_scoring_jobs_pass_threshold(scorer):
    """
    Run the scorer over the greenhouse test fixture and verify that the jobs
    the pipeline is expected to qualify (Data Scientist, Algorithm Developer)
    score above the 60-pt threshold.
    """
    import responses as resp_mock

    from models.job import CompanyConfig
    from scrapers.greenhouse import GreenhouseScraper, _API_BASE

    company = CompanyConfig(name="TestCo", ats="greenhouse", slug="testco", enabled=True)
    scraper = GreenhouseScraper(request_delay=0)

    with open(FIXTURE_PATH) as f:
        fixture = json.load(f)

    with resp_mock.RequestsMock() as rsps:
        rsps.add(resp_mock.GET, f"{_API_BASE}/testco/jobs", json=fixture, status=200)
        raw_jobs = scraper.fetch_jobs(company, max_age_days=60)

    scores = {j.title: scorer.score(j) for j in raw_jobs}

    # Data Scientist: Python + PyTorch + Tel Aviv + 3 yrs of experience -> should score high
    assert scores["Data Scientist"].score >= 70, (
        f"Expected >=70 for Data Scientist, got {scores['Data Scientist'].score}"
    )

    # Algorithm Developer: Herzliya (tier-1 + train) + hybrid + algorithm keywords -> high
    assert scores["Algorithm Developer"].score >= 60, (
        f"Expected >=60 for Algorithm Developer, got {scores['Algorithm Developer'].score}"
    )
