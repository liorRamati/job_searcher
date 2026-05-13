"""
Smoke tests - quick sanity checks on live APIs before a full run.

Run with: pytest -m smoke
"""

import pytest


@pytest.mark.smoke
def test_greenhouse_taboola_accessible():
    """Verify Taboola's Greenhouse board is accessible."""
    import requests
    resp = requests.get(
        "https://boards-api.greenhouse.io/v1/boards/taboola/jobs",
        params={"content": "true"},
        timeout=10,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "jobs" in data


@pytest.mark.smoke
def test_greenhouse_jfrog_accessible():
    """Verify JFrog's Greenhouse board is accessible."""
    import requests
    resp = requests.get(
        "https://boards-api.greenhouse.io/v1/boards/jfrog/jobs",
        params={"content": "true"},
        timeout=10,
    )
    assert resp.status_code == 200


@pytest.mark.smoke
def test_config_files_valid():
    """Verify all config files can be loaded."""
    import yaml

    with open("config/settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    assert "agent" in settings
    assert "search" in settings

    with open("config/companies.yaml", encoding="utf-8") as f:
        companies = yaml.safe_load(f)
    assert "companies" in companies

    with open("config/locations.yaml", encoding="utf-8") as f:
        locations = yaml.safe_load(f)
    assert "areas" in locations

    with open("config/tech_keywords.yaml", encoding="utf-8") as f:
        tech = yaml.safe_load(f)


@pytest.mark.smoke
def test_profile_file_valid():
    """Verify user profile can be loaded."""
    from models.profile import UserProfile
    profile = UserProfile.from_json("tests/fixtures/user_profile.json")
    assert profile.name
    assert profile.target_titles
    assert profile.skills


@pytest.mark.smoke
def test_scorer_initialization():
    """Verify scorer can be initialized."""
    import yaml
    from models.profile import UserProfile, UserSkills

    locations = yaml.safe_load(open("config/locations.yaml", encoding="utf-8"))
    tech_keywords = yaml.safe_load(open("config/tech_keywords.yaml", encoding="utf-8")) or {}
    profile = UserProfile(
        name="Test",
        seniority="mid",
        years_of_experience=3,
        target_titles=["data scientist"],
        skills=UserSkills(languages=["Python"]),
    )

    from pipeline.scorer import Scorer
    scorer = Scorer(profile, locations, tech_keywords, strict=True)
    assert scorer is not None


@pytest.mark.smoke
def test_disqualifier_initialization():
    """Verify disqualifier can be initialized."""
    import yaml
    from models.profile import UserProfile, UserSkills

    locations = yaml.safe_load(open("config/locations.yaml", encoding="utf-8"))
    profile = UserProfile(
        name="Test",
        seniority="mid",
        years_of_experience=3,
        target_titles=["data scientist"],
        skills=UserSkills(languages=["Python"]),
    )

    from pipeline.disqualifier import Disqualifier
    dq = Disqualifier(locations, profile)
    assert dq is not None