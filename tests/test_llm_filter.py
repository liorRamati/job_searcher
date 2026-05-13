"""Unit tests for LLMFilter — verifies that LLM output is used to filter jobs."""

import re
import pytest
from models.job import ScoredJob
from models.profile import UserProfile
from pipeline.llm_filter import LLMFilter, _build_hard_rules, _build_system_prompt


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_job(title: str, score: float = 70.0) -> ScoredJob:
    return ScoredJob(
        id=f"test:{title}",
        title=title,
        company="TestCo",
        url=f"https://example.com/{title.replace(' ', '-')}",
        location_raw="Tel Aviv",
        description_html=f"<p>{title} job description</p>",
        source="test",
        score=score,
    )


class _FakeLLM:
    """Fake LLM that echoes back canned verdicts.

    verdicts maps 1-based job number (within a batch) to a relevant bool.
    The fake parses the batch size from the prompt and builds valid JSON.
    """

    def __init__(self, verdicts: dict[int, bool]):
        self._verdicts = verdicts

    def complete(self, prompt: str, system_message: str = "", **kwargs) -> str:
        numbers = re.findall(r"^(\d+)\.", prompt, re.MULTILINE)
        jobs_json = []
        for n_str in numbers:
            n = int(n_str)
            relevant = self._verdicts.get(n, True)
            relevant_str = "true" if relevant else "false"
            jobs_json.append(
                f'{{"number": {n}, "relevant": {relevant_str}, '
                f'"reason": "test", "requirements": "", "tech_stack": ""}}'
            )
        return '{{"jobs": [{jobs}]}}'.format(jobs=", ".join(jobs_json))


@pytest.fixture
def profile():
    return UserProfile.from_json("tests/fixtures/user_profile.json")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFilterJobsUsesLLMOutput:
    def test_irrelevant_jobs_are_excluded(self, profile):
        """Jobs the LLM marks irrelevant must not appear in the output."""
        jobs = [_make_job("Data Scientist"), _make_job("C++ Developer"), _make_job("ML Engineer")]
        # LLM says job 2 (C++ Developer) is irrelevant
        fake_llm = _FakeLLM({1: True, 2: False, 3: True})
        llm_filter = LLMFilter(fake_llm, batch_size=10)

        result = llm_filter.filter_jobs(jobs, profile)

        titles = [j.title for j in result]
        assert "Data Scientist" in titles
        assert "ML Engineer" in titles
        assert "C++ Developer" not in titles

    def test_all_relevant_returns_all(self, profile):
        jobs = [_make_job("Data Scientist"), _make_job("ML Engineer")]
        fake_llm = _FakeLLM({1: True, 2: True})
        result = LLMFilter(fake_llm, batch_size=10).filter_jobs(jobs, profile)
        assert len(result) == 2

    def test_all_irrelevant_returns_empty(self, profile):
        jobs = [_make_job("C++ Developer"), _make_job("Frontend Engineer")]
        fake_llm = _FakeLLM({1: False, 2: False})
        result = LLMFilter(fake_llm, batch_size=10).filter_jobs(jobs, profile)
        assert result == []

    def test_batch_splitting_preserves_verdicts(self, profile):
        """Batching must not mix up which verdict belongs to which job."""
        jobs = [_make_job(f"Job {i}") for i in range(1, 6)]
        # Within each batch of 2: job number 1 = relevant, job number 2 = irrelevant
        # batch 1: Job1(→relevant), Job2(→irrelevant)
        # batch 2: Job3(→relevant), Job4(→irrelevant)
        # batch 3: Job5(→relevant, only 1 job so number 1)
        fake_llm = _FakeLLM({1: True, 2: False})
        result = LLMFilter(fake_llm, batch_size=2).filter_jobs(jobs, profile)

        titles = [j.title for j in result]
        assert "Job 1" in titles
        assert "Job 3" in titles
        assert "Job 5" in titles
        assert "Job 2" not in titles
        assert "Job 4" not in titles

    def test_llm_parse_failure_keeps_all(self, profile):
        """If the LLM response can't be parsed, all jobs in the batch are kept (safe default)."""
        class _BrokenLLM:
            def complete(self, *a, **kw):
                return "this is not json at all"

        jobs = [_make_job("Data Scientist"), _make_job("ML Engineer")]
        result = LLMFilter(_BrokenLLM(), batch_size=10).filter_jobs(jobs, profile)
        assert len(result) == 2

    def test_empty_input_returns_empty(self, profile):
        result = LLMFilter(_FakeLLM({}), batch_size=10).filter_jobs([], profile)
        assert result == []

    def test_min_score_below_threshold_passes_through_unclassified(self, profile):
        """Jobs below min_score_before_llm skip LLM and are always kept."""
        low_score_job = _make_job("Anything", score=10.0)
        high_score_job = _make_job("Data Scientist", score=70.0)

        # LLM rejects the only job it sees (the high-score one, batch position 1)
        fake_llm = _FakeLLM({1: False})
        result = LLMFilter(fake_llm, batch_size=10, min_score_before_llm=50).filter_jobs(
            [low_score_job, high_score_job], profile
        )

        titles = [j.title for j in result]
        assert "Anything" in titles          # passed through (score < min_score)
        assert "Data Scientist" not in titles  # LLM rejected it


class TestBuildHardRules:
    def test_contains_excluded_titles(self, profile):
        rules = _build_hard_rules(profile)
        assert "Manager" in rules
        assert "Director" in rules

    def test_contains_excluded_keywords(self, profile):
        rules = _build_hard_rules(profile)
        assert "FPGA" in rules

    def test_contains_dominant_stack_threshold(self, profile):
        rules = _build_hard_rules(profile)
        assert "c++" in rules.lower()
        assert ">= 4" in rules

    def test_contains_fewer_than_threshold_nuance(self, profile):
        """The 'occasional mention = OK' nuance must be present."""
        rules = _build_hard_rules(profile)
        assert "Fewer than 4" in rules

    def test_system_prompt_has_user_target_roles(self, profile):
        prompt = _build_system_prompt(profile)
        assert "data scientist" in prompt.lower()
        assert "IRRELEVANT" in prompt
