"""Unit and integration tests for pipeline/cover_letter.py"""

import pytest
from unittest.mock import Mock, MagicMock

from models.job import ScoredJob, RawJob
from models.profile import UserProfile, UserSkills, HardDisqualifiers
from pipeline.cover_letter import (
    CoverLetterGenerator,
    generate_cover_letter,
    generate_cover_letters,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_llm():
    """Mock LLM client that returns a predefined response."""
    llm = Mock()
    llm.complete = Mock(
        return_value="""Dear Taboola Team,

I am excited to apply for the Data Scientist position at Taboola. With my background in machine learning and experience with large-scale data pipelines, I am confident I can contribute to your team's success.

In my current role, I have developed recommendation models using Python and PyTorch, directly aligning with your requirements for experience with ML frameworks and data-driven products. I have also worked extensively with SQL and cloud platforms, matching your need for strong database and infrastructure skills.

I would love to discuss how my background aligns with your needs. Thank you for considering my application.

Best regards,
Lior Ramati"""
    )
    return llm


@pytest.fixture
def profile():
    """Test user profile."""
    return UserProfile(
        name="Lior Ramati",
        seniority="mid",
        years_of_experience=3,
        target_titles=["data scientist", "algorithm developer", "ml engineer"],
        skills=UserSkills(
            languages=["Python", "SQL"],
            frameworks=["PyTorch", "Pandas", "NumPy", "scikit-learn"],
            tools=["Git", "Docker"],
            domains=["machine learning", "data science"],
        ),
        hard_disqualifiers=HardDisqualifiers(
            excluded_titles=["Manager", "Director", "VP"],
            excluded_keywords=["FPGA", "PHP"],
            min_years_required_max=7,
        ),
    )


@pytest.fixture
def scored_job():
    """Test scored job."""
    return ScoredJob(
        id="greenhouse:12345",
        title="Data Scientist",
        company="Taboola",
        url="https://boards.greenhouse.io/taboola/jobs/12345",
        location_raw="Tel Aviv, Israel",
        description_html="<p>We are looking for a Data Scientist to join our team.</p><p>Requirements: Python, PyTorch, SQL, 3+ years experience</p>",
        source="greenhouse",
        score=75.0,
        score_breakdown={"title": 22.5, "tech_stack": 25.0, "location": 20.0, "work_type": 8.0, "requirements": 0.0},
        work_type="hybrid",
        tech_stack_found=["Python", "PyTorch", "SQL"],
    )


# ── Unit tests ───────────────────────────────────────────────────────────────

class TestCoverLetterGenerator:
    """Tests for CoverLetterGenerator class."""

    def test_generate_calls_llm(self, mock_llm):
        """Verify that generate() calls the LLM with correct parameters."""
        generator = CoverLetterGenerator(mock_llm)

        result = generator.generate(
            company="Taboola",
            title="Data Scientist",
            location="Tel Aviv, Israel",
            description="Looking for a Data Scientist with Python and PyTorch experience.",
            user_name="Lior Ramati",
            skills=["Python", "SQL", "PyTorch"],
            years_of_experience=3,
            target_titles=["data scientist", "ml engineer"],
        )

        mock_llm.complete.assert_called_once()
        call_args = mock_llm.complete.call_args
        assert "Taboola" in call_args.kwargs["prompt"]
        assert "Data Scientist" in call_args.kwargs["prompt"]
        assert "Lior Ramati" in call_args.kwargs["prompt"]

    def test_generate_returns_string(self, mock_llm):
        """Verify that generate() returns a string."""
        generator = CoverLetterGenerator(mock_llm)

        result = generator.generate(
            company="TestCo",
            title="Software Engineer",
            location="Tel Aviv",
            description="Test description",
            user_name="Test User",
            skills=["Python"],
            years_of_experience=2,
            target_titles=["software engineer"],
        )

        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_handles_missing_location(self, mock_llm):
        """Verify that generate() handles None location gracefully."""
        generator = CoverLetterGenerator(mock_llm)

        result = generator.generate(
            company="TestCo",
            title="Engineer",
            location=None,
            description="Test",
            user_name="User",
            skills=["Python"],
            years_of_experience=1,
            target_titles=["engineer"],
        )

        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_truncates_long_description(self, mock_llm):
        """Verify that very long descriptions are truncated."""
        generator = CoverLetterGenerator(mock_llm)

        long_description = "A" * 5000

        generator.generate(
            company="TestCo",
            title="Engineer",
            location="Tel Aviv",
            description=long_description,
            user_name="User",
            skills=["Python"],
            years_of_experience=1,
            target_titles=["engineer"],
        )

        call_args = mock_llm.complete.call_args
        prompt = call_args.kwargs["prompt"]
        assert len(prompt) < 5000


class TestGenerateCoverLetter:
    """Tests for the convenience function generate_cover_letter()."""

    def test_generate_from_job_and_profile(self, mock_llm, profile, scored_job):
        """Test generating cover letter from ScoredJob and UserProfile."""
        result = generate_cover_letter(scored_job, profile, mock_llm)

        assert isinstance(result, str)
        assert len(result) > 0

    def test_uses_job_company_in_prompt(self, mock_llm, profile, scored_job):
        """Verify the job's company appears in the LLM prompt."""
        generate_cover_letter(scored_job, profile, mock_llm)

        call_args = mock_llm.complete.call_args
        assert scored_job.company in call_args.kwargs["prompt"]

    def test_uses_job_title_in_prompt(self, mock_llm, profile, scored_job):
        """Verify the job's title appears in the LLM prompt."""
        generate_cover_letter(scored_job, profile, mock_llm)

        call_args = mock_llm.complete.call_args
        assert scored_job.title in call_args.kwargs["prompt"]


class TestGenerateCoverLetters:
    """Tests for the batch function generate_cover_letters()."""

    def test_generates_for_qualified_jobs(self, mock_llm, profile):
        """Test that cover letters are generated for jobs above threshold."""
        jobs = [
            ScoredJob(
                id="1", title="Engineer", company="A", url="http://a.com",
                source="greenhouse", score=70.0, description_html="desc",
            ),
            ScoredJob(
                id="2", title="Manager", company="B", url="http://b.com",
                source="greenhouse", score=55.0, description_html="desc",
            ),
        ]

        results = generate_cover_letters(jobs, profile, mock_llm, score_threshold=60)

        assert results[0].cover_letter is not None
        assert results[1].cover_letter is None

    def test_respects_score_threshold(self, mock_llm, profile):
        """Test that jobs below threshold don't get cover letters."""
        jobs = [
            ScoredJob(
                id="1", title="Engineer", company="A", url="http://a.com",
                source="greenhouse", score=59.0, description_html="desc",
            ),
            ScoredJob(
                id="2", title="Engineer", company="B", url="http://b.com",
                source="greenhouse", score=60.0, description_html="desc",
            ),
        ]

        results = generate_cover_letters(jobs, profile, mock_llm, score_threshold=60)

        assert results[0].cover_letter is None
        assert results[1].cover_letter is not None

    def test_empty_job_list(self, mock_llm, profile):
        """Test handling of empty job list."""
        results = generate_cover_letters([], profile, mock_llm)
        assert results == []

    def test_default_threshold(self, mock_llm, profile):
        """Test default threshold of 60."""
        jobs = [
            ScoredJob(
                id="1", title="Engineer", company="A", url="http://a.com",
                source="greenhouse", score=60.0, description_html="desc",
            ),
        ]

        results = generate_cover_letters(jobs, profile, mock_llm)
        assert results[0].cover_letter is not None


# ── Integration tests ────────────────────────────────────────────────────────

@pytest.mark.integration
class TestCoverLetterIntegration:
    """Integration tests that would require a live LLM (skipped in normal runs)."""

    @pytest.mark.skip(reason="Requires live LLM endpoint")
    def test_real_llm_integration(self, profile, scored_job):
        """Test with a real LM Studio or Claude endpoint."""
        from llm.lm_studio_client import LMStudioClient

        llm = LMStudioClient()
        result = generate_cover_letter(scored_job, profile, llm)

        assert isinstance(result, str)
        assert len(result) > 100
        assert "Taboola" in result or "dear" in result.lower()