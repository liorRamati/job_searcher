"""
Cover letter generator — creates personalized cover letters for qualifying jobs.

Uses LLM to generate 3-4 paragraph cover letters that reference:
- The company name and job title
- 2-3 specific requirements from the job description
- The user's relevant skills

Only generates cover letters for jobs with score >= threshold (default 60).
"""

from __future__ import annotations

import logging
from typing import Optional

from llm.base import BaseLLM

_log = logging.getLogger("job_searcher.cover_letter")


COVER_LETTER_SYSTEM_PROMPT = """You are a professional cover letter writer. Write a concise, personalized cover letter for a job application.

Guidelines:
- 3-4 paragraphs, professional tone
- Start with "Dear Hiring Team" or "Dear [Company Name] Team"
- First paragraph: Express interest in the specific role at the specific company
- Middle paragraphs: Highlight 2-3 specific requirements from the job that match the user's skills
- Final paragraph: Call to action, express enthusiasm, thank the reader
- Keep it under 300 words
- Do not use placeholder text like [insert name]
- Write in English"""


COVER_LETTER_PROMPT_TEMPLATE = """Generate a cover letter for the following job application:

Company: {company}
Job Title: {title}
Location: {location}

Job Description (key requirements):
{description}

User's Profile:
- Name: {user_name}
- Skills: {skills}
- Years of Experience: {years_exp}
- Target Titles: {target_titles}

Write a personalized cover letter that connects the user's background to the job requirements.
"""


class CoverLetterGenerator:
    """Generates personalized cover letters for job applications."""

    def __init__(self, llm_client: BaseLLM):
        """
        Initialize the cover letter generator.

        Args:
            llm_client: An LLM client instance (BaseLLM).
        """
        self.llm = llm_client

    def generate(
        self,
        company: str,
        title: str,
        location: Optional[str],
        description: str,
        user_name: str,
        skills: list[str],
        years_of_experience: int,
        target_titles: list[str],
        max_tokens: int = 1500,
    ) -> str:
        """
        Generate a cover letter for a job.

        Args:
            company: Company name.
            title: Job title.
            location: Job location.
            description: Job description text.
            user_name: User's full name.
            skills: List of user's skills.
            years_of_experience: User's years of experience.
            target_titles: User's target job titles.
            max_tokens: Maximum tokens for generation.

        Returns:
            Generated cover letter text.
        """
        description_preview = description[:2000] if description else "No description available"

        prompt = COVER_LETTER_PROMPT_TEMPLATE.format(
            company=company,
            title=title,
            location=location or "Not specified",
            description=description_preview,
            user_name=user_name,
            skills=", ".join(skills),
            years_exp=years_of_experience,
            target_titles=", ".join(target_titles),
        )

        try:
            response = self.llm.complete(
                prompt=prompt,
                system_message=COVER_LETTER_SYSTEM_PROMPT,
                max_tokens=max_tokens,
                temperature=0.7,
            )
        except Exception as exc:
            _log.error(
                f"LLM call failed while generating cover letter for {title} @ {company}: {exc}",
                exc_info=True,
            )
            return ""

        return response.strip()


def generate_cover_letter(
    job,
    profile,
    llm_client: BaseLLM,
) -> str:
    """
    Convenience function to generate a cover letter from a ScoredJob and UserProfile.

    Args:
        job: A ScoredJob instance.
        profile: A UserProfile instance.
        llm_client: An LLM client instance.

    Returns:
        Generated cover letter text.
    """
    generator = CoverLetterGenerator(llm_client)

    user_skills = profile.skills.all_skills() if profile.skills else []

    return generator.generate(
        company=job.company,
        title=job.title,
        location=job.location_raw,
        description=job.description_html,
        user_name=profile.name,
        skills=user_skills,
        years_of_experience=profile.years_of_experience,
        target_titles=profile.target_titles,
    )


def generate_cover_letters(
    jobs: list,
    profile,
    llm_client: BaseLLM,
    score_threshold: int = 60,
) -> list:
    """
    Generate cover letters for all qualifying jobs.

    Args:
        jobs: List of ScoredJob instances.
        profile: A UserProfile instance.
        llm_client: An LLM client instance.
        score_threshold: Minimum score to generate a cover letter for.

    Returns:
        List of ScoredJob instances, with cover_letter populated for those above threshold.
    """
    total = len(jobs)
    eligible = sum(1 for j in jobs if j.score >= score_threshold)
    _log.info(f"Generating cover letters for {eligible}/{total} jobs (score ≥ {score_threshold})")

    results = []
    for job in jobs:
        if job.score >= score_threshold:
            _log.debug(f"  Generating cover letter: {job.title} @ {job.company} (score {job.score:.0f})")
            letter = generate_cover_letter(job, profile, llm_client)
            if letter:
                job.cover_letter = letter
                _log.debug(f"  Done: {job.title} @ {job.company}")
            else:
                _log.warning(f"Cover letter generation returned empty result for {job.title} @ {job.company}")
        results.append(job)

    generated = sum(1 for j in results if j.cover_letter)
    _log.info(f"Cover letters generated: {generated}/{eligible}")
    return results