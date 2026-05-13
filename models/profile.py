"""
UserProfile model — the user's skills, target titles, and hard disqualifiers.

The profile is loaded from config/profile.json at startup and shared (read-only)
by both the Disqualifier (hard rules) and the Scorer (soft scoring).

You can generate profile.json automatically from a PDF/Word resume:
    python main.py --parse-resume resume.pdf

Or maintain it by hand — the JSON structure mirrors the fields in this module.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel


class UserSkills(BaseModel):
    """
    All of the user's skills, split by type so the scorer can weight them
    separately if needed in the future.
    """
    languages: list[str] = []   # e.g. ["Python", "SQL"]
    frameworks: list[str] = []  # e.g. ["PyTorch", "Pandas"]
    tools: list[str] = []       # e.g. ["Git", "Docker"]
    domains: list[str] = []     # e.g. ["machine learning", "data science"]

    def all_skills(self) -> list[str]:
        """Flat list of every skill across all categories."""
        return self.languages + self.frameworks + self.tools + self.domains


class DominantTechStack(BaseModel):
    """
    A tech group that disqualifies a job when it is the *primary* focus.

    Unlike excluded_keywords (any single mention = out), this rule fires only
    when the technology clearly dominates the job requirements. It allows
    passing mentions like "some C++ knowledge is a plus" while rejecting jobs
    where C++ or React is what you'd spend 80% of your time on.

    Two independent signals — either one is enough to disqualify:
      1. title_keywords: any of these appearing in the job title is a strong,
         unambiguous signal (e.g. "React Developer", "C++ Engineer").
      2. body_keywords + body_threshold: count total occurrences of all body
         keywords in the description; if the count reaches the threshold the
         job is dominated by that stack.
    """
    name: str                       # display name shown in the disqualify reason
    title_keywords: list[str] = []  # checked as case-insensitive substrings of the title
    body_keywords: list[str] = []   # each occurrence of each keyword is counted
    body_threshold: int = 4         # total occurrences needed to declare dominance


class HardDisqualifiers(BaseModel):
    """
    Absolute deal-breakers. Jobs matching any of these are dropped by the
    Disqualifier *before* scoring — they never appear in the results.

    Keep these lists short and conservative. Over-aggressive disqualification
    means missed opportunities. When unsure, leave it out and let the scorer
    penalise instead.

    Rule hierarchy (checked in order, short-circuits on first match):
      excluded_titles          → role-level title check (Manager, VP, ...)
      excluded_title_keywords  → tech-role title check (frontend, full-stack, ...)
      excluded_keywords        → binary keyword kill-switch (FPGA, PHP, ...)
      dominant_tech_stacks     → dominance check (C++ / JS as primary stack)
      min_years_required_max   → seniority hard limit
    """
    # Job title phrases indicating a seniority/role mismatch.
    # Matched with word-boundary regex so "VP" won't fire on "MVP".
    excluded_titles: list[str] = []

    # Tech-stack or role-type words in the job *title* that indicate the user's
    # unwanted stack is the primary focus. Matched as case-insensitive substrings.
    # Examples: "frontend", "full-stack", "ui engineer"
    # These are separate from excluded_titles because they're about technology
    # direction, not seniority level, and they don't need word-boundary matching.
    excluded_title_keywords: list[str] = []

    # Technologies whose *presence anywhere* means the job is outside the user's stack.
    # Reserved for absolute deal-breakers where even a passing mention is disqualifying
    # (e.g. FPGA — hardware-specific, no overlap with data science).
    # Note: C++ and JS/TS belong in dominant_tech_stacks, not here, because
    # "some C++ for perf-critical paths" is acceptable.
    excluded_keywords: list[str] = []

    # Role-type keywords that disqualify when they appear in the job TITLE.
    # These are business roles (sales, marketing, etc.) that are irrelevant to
    # technical data science roles. Unlike excluded_keywords, these only check
    # the title, not the description, because any company might mention "growth"
    # in their job descriptions.
    excluded_title_keywords_role: list[str] = []

    # Tech groups that disqualify when they are the job's dominant stack.
    # Allows passing mentions while blocking jobs where the tech is the primary work.
    dominant_tech_stacks: list[DominantTechStack] = []

    # If a job states a years-of-experience requirement higher than this, it's
    # too senior to apply to. Set conservatively — a 3-year candidate won't get
    # an interview for a 10+ year role, but might stretch to 5-6 years.
    min_years_required_max: int = 10


class UserProfile(BaseModel):
    """Complete user profile used by both the Disqualifier and the Scorer."""
    name: str
    seniority: str = "mid"              # junior | mid | senior
    years_of_experience: int = 3
    previous_titles: list[str] = []
    target_titles: list[str] = []       # preferred job titles, lowercase
    skills: UserSkills = UserSkills()
    hard_disqualifiers: HardDisqualifiers = HardDisqualifiers()

    @classmethod
    def from_json(cls, path: str) -> "UserProfile":
        """Load a UserProfile from a JSON file (e.g. tests/fixtures/user_profile.json)."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)
