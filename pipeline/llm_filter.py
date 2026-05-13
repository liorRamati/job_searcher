"""
LLM-based job filter — uses the configured LLM to classify jobs as relevant or irrelevant.

Usage:
    from pipeline.llm_filter import LLMFilter
    llm_filter = LLMFilter(llm_client, batch_size=10)
    relevant_jobs = llm_filter.filter_jobs(jobs, profile)
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Optional, List

if TYPE_CHECKING:
    from llm.base import BaseLLM

from models.job import ScoredJob
from models.profile import UserProfile

_log = logging.getLogger("job_searcher.llm_filter")


_SYSTEM_PROMPT_BASE = """You are a job relevance filter for a {previous_titles} candidate.

## Candidate profile
- Target roles: {target_roles}
- Domain expertise: {domains}
- Technical skills: {skills}
- Seniority: {seniority} ({years_exp} years of total experience)

Apply the steps below in order. Stop at the first step that produces a definitive verdict.

---

## STEP 1 — Location filter
The candidate only considers jobs in these areas: {locations}

Use your geographic knowledge to apply this strictly:
- If the job specifies a city or region that is clearly outside these areas, mark IRRELEVANT.
- If the city is within or geographically adjacent to one of these areas (even if not named explicitly), mark RELEVANT.
- If the location is missing, just the country name, or otherwise too vague to determine → mark RELEVANT.
Be decisive: a clearly distant location is IRRELEVANT; a nearby or adjacent city is RELEVANT; no location is RELEVANT.

---

## STEP 2 — Hard disqualifiers
Mark IRRELEVANT immediately if any of the following fire:
{hard_rules}

---

## STEP 3 — Role category alignment
Infer the professional category of the candidate from the target roles and domain expertise above.
Then determine the *primary function* of this job — what the person hired will spend the majority of their working time doing.

If the job's primary function clearly places it in a different professional category than the candidate's target roles, mark IRRELEVANT.
A different professional category means the day-to-day work is fundamentally different in type — not just in technology or subdomain — from what the candidate's target roles involve.

If the job title is generic (e.g. "Software Engineer", "Researcher") or spans disciplines, read the description to identify the primary function before deciding.
When the primary function is ambiguous or could plausibly align with the candidate's target roles, proceed to Step 4.

---

## STEP 4 — Experience and seniority fit
Mark IRRELEVANT only when the gap is clearly too large to bridge:
- The job states an explicit minimum-years requirement that substantially exceeds the candidate's {years_exp} years, or requires that many years specifically in a narrow domain the candidate lacks
- The job is titled Staff / Principal / Distinguished and the description confirms deep specialisation far beyond {seniority} level
Do NOT reject for a single missing skill or tool, but DO reject when the experience gap is clearly unbridgeable.
When uncertain, keep RELEVANT.

---

## STEP 5 — Domain and tech stack fit
Check whether the primary technical work of this job aligns with the candidate's domains and skills:
- Primary technical work matches the candidate's domains or target roles → RELEVANT
- Generic title: inspect description for primary technical work → if it aligns with candidate's domains → RELEVANT
- Primary technical work is in a clearly unrelated domain with no meaningful overlap → IRRELEVANT
When uncertain about domain alignment, keep RELEVANT — the candidate will judge the role.

---

Respond with valid JSON only. No explanation outside the JSON.

Format:
{{
  "jobs": [
    {{
      "number": <job_number>,
      "primary_function": "<brief phrase: the primary daily work of this role>",
      "relevant": true|false,
      "reason": "<one sentence: which step decided and why>",
      "requirements": "<key qualifications from the posting, or empty string>",
      "tech_stack": "<comma-separated technologies mentioned, or empty string>"
    }},
    ...
  ]
}}"""

_USER_PROMPT_TEMPLATE = """Jobs to classify:
{jobs_list}

For each job apply Steps 1–5 from the system prompt in order. Output only valid JSON — no other text."""


def _build_hard_rules(profile: UserProfile) -> str:
    """Build the hard-rules section of the prompt entirely from the user's profile config."""
    dq = profile.hard_disqualifiers
    lines = []

    if dq.excluded_titles:
        lines.append(
            f"- Title contains an excluded role pattern "
            f"({', '.join(dq.excluded_titles)}) → IRRELEVANT"
        )

    if dq.excluded_title_keywords:
        lines.append(
            f"- Title contains an excluded tech/role keyword "
            f"({', '.join(dq.excluded_title_keywords)}) → IRRELEVANT"
        )

    excluded_role_kw = getattr(dq, "excluded_title_keywords_role", [])
    if excluded_role_kw:
        lines.append(
            f"- Title indicates a non-technical business role "
            f"({', '.join(excluded_role_kw)}) → IRRELEVANT"
        )

    if dq.excluded_keywords:
        lines.append(
            f"- Any of these appear anywhere in the title or description "
            f"({', '.join(dq.excluded_keywords)}) → IRRELEVANT regardless of context"
        )

    for stack in getattr(dq, "dominant_tech_stacks", []):
        title_signals = ", ".join(f'"{k}"' for k in stack.title_keywords)
        body_signals = ", ".join(stack.body_keywords)
        lines.append(
            f"- {stack.name} as PRIMARY focus → IRRELEVANT. "
            f"Title signals (any one is decisive): [{title_signals}]. "
            f"Description signals: if [{body_signals}] clearly dominate the required skills "
            f"— appearing repeatedly as core requirements, not just occasional mentions or optional bonuses — → IRRELEVANT. "
            f"If mentioned only as a passing reference or 'nice-to-have' → still RELEVANT."
        )

    max_years = getattr(dq, "min_years_required_max", None)
    if max_years:
        lines.append(
            f"- Job explicitly states a minimum of more than {max_years} years of experience → IRRELEVANT"
        )

    return "\n".join(lines) if lines else "(no hard rules configured)"


def _build_system_prompt(profile: UserProfile, locations: List[str]) -> str:
    skills = profile.skills
    non_domain_skills = skills.languages + skills.frameworks + skills.tools
    return _SYSTEM_PROMPT_BASE.format(
        previous_titles=", ".join(profile.previous_titles),
        target_roles=", ".join(profile.target_titles),
        domains=", ".join(skills.domains) if skills.domains else "general software engineering",
        skills=", ".join(non_domain_skills) if non_domain_skills else "general",
        seniority=profile.seniority,
        years_exp=profile.years_of_experience,
        locations=", ".join(locations),
        hard_rules=_build_hard_rules(profile),
    )


def _build_user_prompt(profile: UserProfile, jobs: list[ScoredJob]) -> str:
    return _USER_PROMPT_TEMPLATE.format(
        jobs_list=_format_job_list(jobs),
    )


def _format_job_list(jobs: list[ScoredJob]) -> str:
    """Format jobs for the LLM prompt as plain text."""
    from output.google_sheets import strip_html
    lines = []
    for i, job in enumerate(jobs, 1):
        desc = strip_html(job.description_html) if job.description_html else "No description"
        lines.append(
            f"{i}. {job.title} @ {job.company} | {job.location_raw or 'N/A'}\n{desc}\n"
        )
    return "\n".join(lines)


def _parse_llm_response(response: str) -> Optional[dict]:
    """Parse LLM JSON response, handling common formatting issues."""
    response = response.strip()
    if response.startswith("```json"):
        response = response[7:]
    if response.startswith("```"):
        response = response[3:]
    if response.endswith("```"):
        response = response[:-3]
    response = response.strip()

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        json_match = re.search(r"\{[\s\S]*\}", response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
    return None


class LLMFilter:
    """LLM-based job classifier."""

    def __init__(
        self,
        llm_client: BaseLLM,
        batch_size: int = 5,
        min_score_before_llm: int = 0,
    ):
        self._llm = llm_client
        self._batch_size = batch_size
        self._min_score = min_score_before_llm

    def filter_jobs(
        self,
        jobs: list[ScoredJob],
        profile: UserProfile,
        locations: list[str],
    ) -> list[ScoredJob]:
        """
        Classify jobs with LLM and return only the relevant ones.

        Jobs below min_score_before_llm are passed through without LLM classification.
        The returned list contains only jobs the LLM marked as relevant (plus pass-through jobs).
        Relevant jobs may have requirements_text and tech_stack_found updated from LLM output.
        """
        if not jobs:
            return []

        to_classify = [j for j in jobs if j.score >= self._min_score]
        pass_through = [j for j in jobs if j.score < self._min_score]

        if not to_classify:
            return pass_through

        system_prompt = _build_system_prompt(profile, locations)
        _log.info(f"LLM classifying {len(to_classify)} jobs...")

        # Collect one verdict dict per job, in the same order as to_classify.
        verdicts: list[dict] = []
        total_batches = (len(to_classify) + self._batch_size - 1) // self._batch_size
        for i in range(0, len(to_classify), self._batch_size):
            batch = to_classify[i : i + self._batch_size]
            batch_num = i // self._batch_size + 1
            _log.info(f"  Batch {batch_num}/{total_batches} ({len(batch)} jobs)...")
            batch_verdicts = self._classify_batch(batch, profile, system_prompt)
            verdicts.extend(batch_verdicts)

        # Build the output list: keep jobs whose verdict is relevant=True.
        relevant: list[ScoredJob] = []
        for job, verdict in zip(to_classify, verdicts):
            if not verdict.get("relevant", True):
                continue
            # Update metadata fields from LLM output using model_copy to avoid
            # mutating the original object (Pydantic models may silently drop direct
            # attribute assignments depending on validation settings).
            updates: dict = {}
            if verdict.get("requirements"):
                updates["requirements_text"] = verdict["requirements"]
            if verdict.get("tech_stack"):
                updates["tech_stack_found"] = [
                    t.strip() for t in verdict["tech_stack"].split(",") if t.strip()
                ]
            job.score_breakdown["llm_reason"] = verdict.get("reason", "")
            job.score_breakdown["llm_primary_function"] = verdict.get("primary_function", "")
            relevant.append(job.model_copy(update=updates) if updates else job)

        _log.info(f"  LLM kept {len(relevant)}/{len(to_classify)} jobs as relevant")
        return pass_through + relevant

    def _classify_batch(
        self,
        batch: list[ScoredJob],
        profile: UserProfile,
        system_prompt: str,
    ) -> list[dict]:
        """
        Call the LLM for one batch and return a list of verdict dicts in batch order.

        Each dict has at minimum: {"relevant": bool}
        Optional keys: "requirements" (str), "tech_stack" (str), "reason" (str).

        On any LLM or parse failure, returns relevant=True for every job in the batch
        so that failures are safe (no false negatives).
        """
        user_prompt = _build_user_prompt(profile, batch)

        try:
            response = self._llm.complete(
                prompt=user_prompt,
                system_message=system_prompt,
                max_tokens=2000,
                temperature=0.3,
            )
        except Exception as exc:
            batch_titles = [f"{j.title} @ {j.company}" for j in batch]
            _log.error(
                f"LLM call failed for batch of {len(batch)} jobs "
                f"({', '.join(batch_titles)}): {exc}. Keeping all as relevant.",
                exc_info=True,
            )
            return [{"relevant": True} for _ in batch]

        result = _parse_llm_response(response)
        if result is None:
            batch_titles = [f"{j.title} @ {j.company}" for j in batch]
            _log.warning(
                f"Failed to parse LLM response for batch of {len(batch)} jobs "
                f"({', '.join(batch_titles)}). "
                f"Keeping all as relevant (safe fallback). "
                f"Raw response (first 500 chars): {response[:500]!r}"
            )
            return [{"relevant": True} for _ in batch]

        # LLM returns 1-based job numbers; build a lookup so we can handle gaps.
        by_number: dict[int, dict] = {
            item.get("number"): item
            for item in result.get("jobs", [])
            if isinstance(item, dict)
        }

        # Return verdicts in the same order as the batch.
        # Default to relevant=True if the LLM omitted a job (safe fallback).
        return [by_number.get(i, {"relevant": True}) for i in range(1, len(batch) + 1)]


def filter_jobs_with_llm(
    jobs: list[ScoredJob],
    profile: UserProfile,
    locations: list[str],
    llm_client: BaseLLM,
    batch_size: int = 5,
    min_score_before_llm: int = 0,
) -> list[ScoredJob]:
    """Convenience wrapper around LLMFilter.filter_jobs."""
    llm_filter = LLMFilter(llm_client, batch_size, min_score_before_llm)
    return llm_filter.filter_jobs(jobs, profile, locations)
