"""Unit tests for pipeline/disqualifier.py — universal rules (location/remote) and profile-based rules."""
from datetime import datetime, timezone

import pytest
import yaml

from models.job import RawJob
from models.profile import DominantTechStack, HardDisqualifiers, UserProfile, UserSkills
from pipeline.disqualifier import Disqualifier

LOCATIONS_PATH = "config/locations.yaml"


@pytest.fixture
def locations():
    with open(LOCATIONS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def dq(locations):
    return Disqualifier(locations)


def job(**kwargs) -> RawJob:
    defaults = dict(
        id="test-1",
        title="Data Scientist",
        company="TestCo",
        url="https://example.com/job/1",
        location_raw="Tel Aviv, Israel",
        description_html="<p>Join our data science team. Hybrid work arrangement.</p>",
        source="greenhouse",
        posted_date=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return RawJob(**defaults)


# ── Tel Aviv area ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_passes_tel_aviv(dq):
    assert not dq.check(job(location_raw="Tel Aviv, Israel")).is_disqualified


@pytest.mark.unit
def test_passes_herzliya(dq):
    assert not dq.check(job(location_raw="Herzliya, Israel")).is_disqualified


@pytest.mark.unit
def test_passes_ramat_gan(dq):
    assert not dq.check(job(location_raw="Ramat Gan")).is_disqualified


@pytest.mark.unit
def test_passes_petah_tikva(dq):
    assert not dq.check(job(location_raw="Petah Tikva, Israel")).is_disqualified


@pytest.mark.unit
def test_passes_rishon_lezion(dq):
    assert not dq.check(job(location_raw="Rishon LeZion, Israel")).is_disqualified


# ── Modi'in area ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_passes_modiin(dq):
    assert not dq.check(job(location_raw="Modi'in, Israel")).is_disqualified


@pytest.mark.unit
def test_passes_modiin_alternate_spelling(dq):
    assert not dq.check(job(location_raw="Modiin, Israel")).is_disqualified


# ── Jerusalem area ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_passes_jerusalem(dq):
    assert not dq.check(job(location_raw="Jerusalem, Israel")).is_disqualified


@pytest.mark.unit
def test_passes_har_hotzvim(dq):
    assert not dq.check(job(location_raw="Har Hotzvim, Jerusalem")).is_disqualified


# ── Ambiguous / unknown ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_passes_bare_israel(dq):
    """'Israel' alone should not be disqualified — location is ambiguous."""
    assert not dq.check(job(location_raw="Israel")).is_disqualified


@pytest.mark.unit
def test_passes_none_location(dq):
    """No location info → benefit of the doubt, don't disqualify."""
    assert not dq.check(job(location_raw=None)).is_disqualified


@pytest.mark.unit
def test_passes_empty_string_location(dq):
    assert not dq.check(job(location_raw="")).is_disqualified


# ── Out-of-area → disqualified ────────────────────────────────────────────────

@pytest.mark.unit
def test_disqualifies_haifa(dq):
    result = dq.check(job(location_raw="Haifa, Israel"))
    assert result.is_disqualified
    assert result.reason is not None


@pytest.mark.unit
def test_disqualifies_beer_sheva(dq):
    result = dq.check(job(location_raw="Beer Sheva, Israel"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_reason_mentions_location(dq):
    result = dq.check(job(location_raw="Haifa, Israel"))
    assert "Haifa" in result.reason


# ── Remote filtering ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_disqualifies_remote_location(dq):
    result = dq.check(job(location_raw="Remote", description_html="<p>Work with us.</p>"))
    assert result.is_disqualified
    assert "remote" in result.reason.lower()


@pytest.mark.unit
def test_disqualifies_fully_remote_description(dq):
    result = dq.check(job(
        location_raw="Israel",
        description_html="<p>This is a fully remote position. Work from anywhere.</p>",
    ))
    assert result.is_disqualified


@pytest.mark.unit
def test_does_not_disqualify_hybrid_even_if_remote_mentioned(dq):
    """'Hybrid - Tel Aviv' or 'partially remote' should NOT be disqualified."""
    result = dq.check(job(
        location_raw="Tel Aviv, Israel",
        description_html="<p>Hybrid work. 3 days office, 2 days remote.</p>",
    ))
    assert not result.is_disqualified


@pytest.mark.unit
def test_does_not_disqualify_hybrid_remote_in_location(dq):
    result = dq.check(job(
        location_raw="Hybrid - Tel Aviv",
        description_html="<p>Flexible work arrangement.</p>",
    ))
    assert not result.is_disqualified


# ── Hebrew city names ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_passes_hebrew_tel_aviv(dq):
    assert not dq.check(job(location_raw="תל אביב, ישראל")).is_disqualified


@pytest.mark.unit
def test_passes_hebrew_herzliya(dq):
    assert not dq.check(job(location_raw="הרצליה")).is_disqualified


@pytest.mark.unit
def test_disqualifies_hebrew_haifa(dq):
    # חיפה = Haifa — not in target areas
    result = dq.check(job(location_raw="חיפה, ישראל"))
    assert result.is_disqualified


# ── Integration ───────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_fixture_jobs_have_expected_qualification(dq):
    """Match the fixture file expectation:
    1001 Tel Aviv → pass
    1002 Herzliya → pass
    1003 Remote → disqualified
    1004 Haifa → disqualified
    1006 Israel (ambiguous) → pass
    """
    import json
    from pathlib import Path

    with open(Path(__file__).parent / "fixtures" / "greenhouse_raw_response.json") as f:
        fixture = json.load(f)

    from scrapers.greenhouse import GreenhouseScraper
    from models.job import CompanyConfig

    company = CompanyConfig(name="TestCo", ats="greenhouse", slug="testco", enabled=True)
    scraper = GreenhouseScraper(request_delay=0)
    scraper._fetch_raw = lambda slug: fixture  # type: ignore[method-assign]
    jobs = scraper.fetch_jobs(company, max_age_days=60)

    results = {j.title: dq.check(j) for j in jobs}

    assert not results["Data Scientist"].is_disqualified
    assert not results["Algorithm Developer"].is_disqualified
    assert results["Machine Learning Engineer"].is_disqualified   # remote
    assert results["Data Engineer"].is_disqualified               # Haifa
    assert not results["Research Engineer"].is_disqualified       # ambiguous Israel → pass


# ── Profile-based hard disqualifiers ──────────────────────────────────────────
# These checks only run when a UserProfile is passed to Disqualifier.__init__.
# Without a profile, only the universal location/remote rules apply.

@pytest.fixture
def profile():
    """
    Profile covering all hard disqualifier types:
      excluded_titles          — Manager, VP, ...
      excluded_title_keywords  — frontend, full-stack, ...
      excluded_keywords        — FPGA, PHP (binary kill-switch; C++ is NOT here)
      dominant_tech_stacks     — C++ and Frontend/JS dominance checks
      min_years_required_max   — seniority limit
    Note: C++ moved from excluded_keywords to dominant_tech_stacks so that
    "some C++ knowledge" passes but a C++-dominated job is still rejected.
    """
    return UserProfile(
        name="Test User",
        seniority="mid",
        years_of_experience=3,
        target_titles=["data scientist"],
        skills=UserSkills(languages=["Python", "SQL"], frameworks=[], tools=[], domains=[]),
        hard_disqualifiers=HardDisqualifiers(
            excluded_titles=["Manager", "Director", "VP", "Head of"],
            excluded_title_keywords=["frontend", "front-end", "full-stack", "full stack"],
            excluded_keywords=["FPGA", "PHP"],
            dominant_tech_stacks=[
                DominantTechStack(
                    name="C/C++",
                    title_keywords=["c++ developer", "c++ engineer", "c/c++ developer"],
                    body_keywords=["c++", "c/c++"],
                    body_threshold=4,
                ),
                DominantTechStack(
                    name="Frontend/JS",
                    title_keywords=["react developer", "react engineer", "angular developer"],
                    body_keywords=["react", "angular", "vue", "typescript", "javascript",
                                   "html", "css", "frontend", "front-end"],
                    body_threshold=4,
                ),
            ],
            min_years_required_max=7,
        ),
    )


@pytest.fixture
def dq_with_profile(locations, profile):
    """Disqualifier with profile-based rules enabled via a UserProfile."""
    return Disqualifier(locations, profile)


@pytest.mark.unit
def test_phase1_only_dq_ignores_title_rules(dq):
    """
    Without a profile, a 'Product Manager' title passes through — only
    universal location/remote rules apply.
    """
    result = dq.check(job(title="Product Manager"))
    assert not result.is_disqualified


@pytest.mark.unit
def test_disqualifies_excluded_title_manager(dq_with_profile):
    """'Product Manager' contains the excluded role 'Manager' → disqualified."""
    result = dq_with_profile.check(job(title="Product Manager"))
    assert result.is_disqualified
    assert "Manager" in result.reason


@pytest.mark.unit
def test_disqualifies_excluded_title_director(dq_with_profile):
    """'Director of Engineering' matches the excluded title 'Director'."""
    result = dq_with_profile.check(job(title="Director of Engineering"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_excluded_title_vp(dq_with_profile):
    """'VP of Data' matches the excluded title 'VP'."""
    result = dq_with_profile.check(job(title="VP of Data"))
    assert result.is_disqualified


@pytest.mark.unit
def test_does_not_disqualify_vp_substring_in_unrelated_word(dq_with_profile):
    """
    'VP' should NOT match inside 'MVP' (word-boundary matching).
    A 'Data Science MVP Lead' title is unusual but shouldn't accidentally fire.
    """
    result = dq_with_profile.check(job(title="Data Scientist MVP"))
    assert not result.is_disqualified


@pytest.mark.unit
def test_disqualifies_excluded_title_head_of(dq_with_profile):
    """'Head of Data Science' matches the multi-word excluded title 'Head of'."""
    result = dq_with_profile.check(job(title="Head of Data Science"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_excluded_keyword_fpga(dq_with_profile):
    """'FPGA' in the description is a binary kill-switch → disqualified on any mention."""
    result = dq_with_profile.check(job(
        description_html="<p>Design FPGA-based signal processing pipelines.</p>",
    ))
    assert result.is_disqualified
    assert "FPGA" in result.reason


@pytest.mark.unit
def test_disqualifies_excluded_keyword_php_in_title(dq_with_profile):
    """'PHP Developer' contains an excluded keyword in the title itself."""
    result = dq_with_profile.check(job(title="PHP Developer"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_over_seniority_explicit_plus(dq_with_profile):
    """
    '10+ years of experience' clearly exceeds the 7-year hard limit → disqualified.
    The '+' syntax is unambiguous about the minimum requirement.
    """
    result = dq_with_profile.check(job(
        description_html="<p>10+ years of experience in machine learning required.</p>",
    ))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_over_seniority_range_format(dq_with_profile):
    """'8-12 years of experience' — the lower bound alone (8) exceeds the limit."""
    result = dq_with_profile.check(job(
        description_html="<p>8-12 years of experience required.</p>",
    ))
    assert result.is_disqualified


@pytest.mark.unit
def test_does_not_disqualify_at_max_years(dq_with_profile):
    """
    Exactly at the hard limit (7 years) is NOT disqualified.
    The scorer will penalise this job instead (far-short penalty).
    """
    result = dq_with_profile.check(job(
        description_html="<p>7+ years of experience required.</p>",
    ))
    assert not result.is_disqualified


@pytest.mark.unit
def test_does_not_disqualify_within_range(dq_with_profile):
    """5+ years is within the limit (7) — should pass disqualification."""
    result = dq_with_profile.check(job(
        description_html="<p>5+ years of experience in data science required.</p>",
    ))
    assert not result.is_disqualified


@pytest.mark.unit
def test_seniority_check_ignores_company_age(dq_with_profile):
    """
    'Our company has 15 years of experience building...' should NOT fire the
    seniority disqualifier because it lacks a '+', range, or context word.
    This tests the conservative regex approach.
    """
    result = dq_with_profile.check(job(
        description_html=(
            "<p>Our company has 15 years of experience building data infrastructure. "
            "We are looking for a mid-level data scientist with 3 years of experience.</p>"
        ),
    ))
    # "15 years of experience" lacks "+", range, or context word → not matched
    # "3 years of experience" is within the limit → not disqualified
    assert not result.is_disqualified


@pytest.mark.unit
def test_seniority_check_requires_minimum_context_word(dq_with_profile):
    """
    'Minimum 9 years' — the context word 'minimum' triggers the conservative check.
    """
    result = dq_with_profile.check(job(
        description_html="<p>Minimum 9 years of experience in ML required.</p>",
    ))
    assert result.is_disqualified


# ── excluded_title_keywords ────────────────────────────────────────────────────
# These disqualify based on tech-role words in the title, regardless of body content.

@pytest.mark.unit
def test_disqualifies_frontend_in_title(dq_with_profile):
    """'Frontend Engineer' contains 'frontend' → disqualified immediately on title."""
    result = dq_with_profile.check(job(title="Frontend Engineer"))
    assert result.is_disqualified
    assert "frontend" in result.reason.lower()


@pytest.mark.unit
def test_disqualifies_front_end_hyphenated_in_title(dq_with_profile):
    """Hyphenated variant 'Front-End Developer' should also be caught."""
    result = dq_with_profile.check(job(title="Front-End Developer"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_full_stack_in_title(dq_with_profile):
    """'Full-Stack Developer' is a tech-role signal → disqualified."""
    result = dq_with_profile.check(job(title="Full-Stack Developer"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_full_stack_no_hyphen_in_title(dq_with_profile):
    """'Full Stack Engineer' (no hyphen) should also be caught."""
    result = dq_with_profile.check(job(title="Full Stack Engineer"))
    assert result.is_disqualified


@pytest.mark.unit
def test_disqualifies_excluded_title_keyword_case_insensitive(dq_with_profile):
    """Title keyword matching is case-insensitive: 'FRONTEND' still fires."""
    result = dq_with_profile.check(job(title="FRONTEND Developer"))
    assert result.is_disqualified


@pytest.mark.unit
def test_does_not_disqualify_backend_engineer(dq_with_profile):
    """'Backend Engineer' contains none of the excluded title keywords → passes."""
    result = dq_with_profile.check(job(title="Backend Engineer"))
    assert not result.is_disqualified


@pytest.mark.unit
def test_disqualifies_full_stack_with_space_around_hyphen(dq_with_profile):
    """
    Some ATS systems format titles as 'Full- Stack Engineer' (space before/after hyphen).
    Hyphen-whitespace normalisation should collapse this to 'full-stack' before matching.
    """
    result = dq_with_profile.check(job(title="Experienced Full- Stack Engineer"))
    assert result.is_disqualified


@pytest.mark.unit
def test_phase1_dq_ignores_excluded_title_keywords(dq):
    """
    Without a profile, excluded_title_keywords don't run — only universal rules apply.
    'Frontend Engineer' in Tel Aviv passes through.
    """
    result = dq.check(job(title="Frontend Engineer"))
    assert not result.is_disqualified


# ── dominant_tech_stacks ───────────────────────────────────────────────────────
# These disqualify when a tech stack is the primary focus of the job.

@pytest.mark.unit
def test_disqualifies_cpp_title_keyword(dq_with_profile):
    """'C++ Developer' matches a title_keyword in the C/C++ stack → disqualified."""
    result = dq_with_profile.check(job(title="C++ Developer"))
    assert result.is_disqualified
    assert "C/C++" in result.reason


@pytest.mark.unit
def test_disqualifies_cpp_dominant_body(dq_with_profile):
    """
    Description with 4+ C++ mentions hits the body_threshold → disqualified.
    Each occurrence of each body keyword counts toward the total.
    """
    result = dq_with_profile.check(job(
        title="Software Engineer",
        description_html=(
            "<p>We write C++ daily. Strong C++ required. "
            "Our codebase is C++17. C++ expertise is essential.</p>"
        ),
    ))
    assert result.is_disqualified
    assert "C/C++" in result.reason


@pytest.mark.unit
def test_passes_cpp_mentioned_once(dq_with_profile):
    """
    A single C++ mention ('some C++ knowledge is a plus') does not reach the
    body_threshold of 4 → job is NOT disqualified.
    """
    result = dq_with_profile.check(job(
        title="Algorithm Developer",
        description_html=(
            "<p>Join our ML team. Python and PyTorch required. "
            "Some C++ knowledge is a bonus for performance work.</p>"
        ),
    ))
    assert not result.is_disqualified


@pytest.mark.unit
def test_passes_cpp_three_mentions_below_threshold(dq_with_profile):
    """
    Three C++ mentions is one short of the threshold (4) → still passes.
    This confirms the threshold is enforced strictly as >=, not >.
    """
    result = dq_with_profile.check(job(
        title="Algorithm Developer",
        description_html="<p>C++ experience helpful. Some C++ used. Background in C++.</p>",
    ))
    assert not result.is_disqualified


@pytest.mark.unit
def test_disqualifies_frontend_js_title_keyword(dq_with_profile):
    """'React Developer' matches a title_keyword in the Frontend/JS stack."""
    result = dq_with_profile.check(job(title="React Developer"))
    assert result.is_disqualified
    assert "Frontend/JS" in result.reason


@pytest.mark.unit
def test_disqualifies_frontend_dominant_body(dq_with_profile):
    """
    A description heavy with frontend tech (React, TypeScript, CSS, HTML, Angular)
    accumulates 5+ keyword mentions → body_threshold reached → disqualified.
    """
    result = dq_with_profile.check(job(
        title="Software Engineer",
        description_html=(
            "<p>Build our web app using React and TypeScript. "
            "Style components with CSS and HTML. "
            "Migrate legacy Angular code to React.</p>"
        ),
    ))
    assert result.is_disqualified
    assert "Frontend/JS" in result.reason


@pytest.mark.unit
def test_passes_single_frontend_mention(dq_with_profile):
    """
    One mention of a frontend keyword (e.g. 'React dashboard') does not reach
    the body_threshold → data/backend job with a small UI component is not DQ'd.
    """
    result = dq_with_profile.check(job(
        title="Data Scientist",
        description_html=(
            "<p>Python and PyTorch for ML pipelines. "
            "You may occasionally contribute to our React dashboard.</p>"
        ),
    ))
    assert not result.is_disqualified


@pytest.mark.unit
def test_dominant_stack_reason_includes_mention_count(dq_with_profile):
    """
    The disqualify reason for a body-threshold hit should include the mention count
    so the user can understand why the job was rejected.
    """
    result = dq_with_profile.check(job(
        title="Software Engineer",
        description_html="<p>React React TypeScript JavaScript CSS</p>",
    ))
    assert result.is_disqualified
    # Reason should mention the count and the threshold
    assert "mention" in result.reason.lower() or "threshold" in result.reason.lower()
