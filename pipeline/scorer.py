"""
Scoring engine — assigns each qualified job a numeric score out of 100.

Scores each job across 5 independent dimensions:

  Dimension        Weight  What it measures
  ─────────────────────────────────────────────────────────────────────────────
  title_similarity   25    How closely the job title matches the user's targets
  tech_stack         30    How many of the user's skills appear in the description
  location           20    Which location tier the job is in (+ train bonus)
  work_type          10    Hybrid preferred, on-site acceptable, unknown neutral
  requirements       15    Years-of-experience alignment

Jobs scoring below the threshold (default 60, set in config/settings.yaml) are
not written to Google Sheets.

Strict mode (default, without --llm-filter): applies required title keywords and a
higher fuzzy-match threshold to filter out clearly irrelevant titles before scoring.

Loose mode (with --llm-filter): relaxes the title filter so the LLM sees a wider
candidate pool including jobs with unconventional but potentially relevant titles.

Standalone usage:
    python -m pipeline.scorer --input qualified.json --profile config/profile.json --output scored.json --verbose
    python -m pipeline.scorer --input qualified.json --min-score 60 --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Optional

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from models.job import RawJob, ScoredJob
from models.profile import UserProfile


# ── Scoring weights ────────────────────────────────────────────────────────────
# These must sum to 100 so the final score is directly interpretable as a percentage.
_W_TITLE        = 25
_W_TECH         = 30
_W_LOCATION     = 20
_W_WORK_TYPE    = 10
_W_REQUIREMENTS = 15

# Title matching thresholds.
# _TITLE_MIN_THRESHOLD and _TITLE_REQUIRED_KEYWORDS are defaults — they are
# overridden by the scoring.title_min_threshold / scoring.title_required_keywords
# values in config/settings.yaml so users can tune them without touching code.
_TITLE_MIN_THRESHOLD = 50
_TITLE_MIN_THRESHOLD_FAST = 30  # internal: lower threshold used in LLM pre-filter mode
_TITLE_REQUIRED_KEYWORDS = ["data", "ml", "machine learning", "algorithm", "scientist", "research", "ai ", "artificial intelligence"]
_TITLE_REQUIRED_KEYWORDS_FAST = []  # internal: no keyword requirement in LLM mode

# Tech stack: default for scoring.tech_full_match_count in settings.yaml.
_TECH_FULL_MATCH_COUNT = 6

# Location: points awarded per tier (defined in config/locations.yaml).
# Tier 1 = Tel Aviv metro (max possible), tiers 2 and 3 receive proportionally less.
_TIER_SCORES: dict[int, int] = {1: 20, 2: 15, 3: 12}
_AMBIGUOUS_LOCATION_SCORE = 10  # "Israel" — vague, not penalised
_UNKNOWN_LOCATION_SCORE   = 10  # no location field at all

# Bonus for jobs in a city with direct Israel Railways access.
# Only makes a visible difference for tier-2/3 cities (tier-1 is already at the cap).
_TRAIN_BONUS = 3

# Work type: points for each detected arrangement.
# Hybrid is the user's preference; on-site is acceptable.
_WORK_HYBRID_SCORE  = 10
_WORK_ONSITE_SCORE  =  8
_WORK_UNKNOWN_SCORE =  6

# Requirements: points based on how the stated years-of-experience aligns with
# the user's actual experience. Values chosen to create meaningful differentiation
# without over-penalising jobs where the user is slightly under-experienced.
_REQ_NO_MENTION =  10   # no years requirement stated — treat as neutral
_REQ_EXACT_MATCH = 15   # required years ≤ user's years_of_experience
_REQ_ONE_SHORT   = 12   # 1 year short of requirement
_REQ_TWO_SHORT   =  8   # 2 years short
_REQ_FAR_SHORT   =  3   # 3+ years short (not hard-disqualified but improbable)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Permissive years-of-experience extractor for soft scoring.
# Matches "3+ years of experience", "3-5 years experience", "3 years exp", etc.
# We take the maximum found value as the most demanding requirement in the job.
_YEARS_EXP_RE = re.compile(
    r"(\d+)\+?\s*(?:-\s*\d+\s*)?years?\s*(?:of\s+)?(?:experience|exp)\b",
    re.IGNORECASE,
)

# Hybrid / on-site detection — same patterns as in disqualifier for consistency
_HYBRID_RE = re.compile(
    r"\bhybrid\b|flexible work|partial remote|partially remote", re.IGNORECASE
)
_ONSITE_RE = re.compile(
    r"\bon[- ]?site\b|\bin[- ]?office\b|in the office", re.IGNORECASE
)


def _strip_html(html: str) -> str:
    """Strip HTML tags and return plain text. Inserts spaces between elements."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ")


class Scorer:
    """
    Converts a RawJob into a ScoredJob by evaluating it against the user's profile.

    The scorer is stateless per-job: all lookups are precomputed in __init__ and
    treated as read-only, so score() is safe to call in a loop.
    """

    def __init__(
        self,
        profile: UserProfile,
        locations: dict,
        tech_keywords: dict,
        strict: bool = True,
        scoring_config: Optional[dict] = None,
    ):
        """
        Parameters
        ----------
        profile        : parsed UserProfile (from config/profile.json)
        locations      : parsed config/locations.yaml
        tech_keywords  : parsed config/tech_keywords.yaml (skill → [alias, ...])
        strict         : if True, apply required title keywords and the configured
                         threshold; if False, use loose pre-filter defaults for LLM mode.
        scoring_config : the 'scoring' section of settings.yaml; overrides the
                         module-level defaults for title_required_keywords,
                         title_min_threshold, and tech_full_match_count.
        """
        self._profile  = profile
        self._locations = locations
        self._strict = strict

        sc = scoring_config or {}
        if strict:
            self._title_threshold   = sc.get("title_min_threshold", _TITLE_MIN_THRESHOLD)
            self._required_keywords = sc.get("title_required_keywords", _TITLE_REQUIRED_KEYWORDS)
        else:
            self._title_threshold   = _TITLE_MIN_THRESHOLD_FAST
            self._required_keywords = _TITLE_REQUIRED_KEYWORDS_FAST
        self._tech_full_match_count = sc.get("tech_full_match_count", _TECH_FULL_MATCH_COUNT)

        self._areas: dict = locations.get("areas", {})

        # Build city → area-name lookup (lowercase keys for cheap comparison).
        # Mirrors the Disqualifier's lookup so location scores are consistent with
        # which jobs made it past disqualification.
        self._city_to_area: dict[str, str] = {}
        for area_name, area_data in self._areas.items():
            for city in area_data.get("cities", []):
                self._city_to_area[city.lower()] = area_name

        # Precompute lowercase train-city set for O(1) membership tests.
        self._train_cities: set[str] = {
            c.lower() for c in locations.get("train_cities", [])
        }

        # Non English → English map, same source as the Disqualifier
        self._non_english: dict = locations.get("non_enlish_city_names", {})

        # Build skill-alias lookup: skill_name → [search_term, ...]
        # This lets us find "PyTorch" even when the description says "torch" or "pytorch".
        # Falls back to the skill name itself if no entry exists in tech_keywords.yaml.
        self._skill_aliases: dict[str, list[str]] = {}
        for skill in profile.skills.all_skills():
            canonical = skill.lower()
            aliases = tech_keywords.get(canonical, [skill.lower()])
            self._skill_aliases[skill] = [a.lower() for a in aliases]

    # ── Public API ─────────────────────────────────────────────────────────────

    def score(self, job: RawJob) -> ScoredJob:
        """
        Score a single job and return a new ScoredJob.

        The original RawJob is not mutated — a new ScoredJob object is created
        by spreading the RawJob fields and adding the scoring results on top.
        """
        title_pts, title_info = self._score_title(job.title)
        tech_pts,  tech_info  = self._score_tech(job.description_html)
        loc_pts,   loc_info   = self._score_location(job.location_raw)
        work_pts,  work_info  = self._score_work_type(job)
        req_pts,   req_info   = self._score_requirements(job.description_html)

        total = title_pts + tech_pts + loc_pts + work_pts + req_pts

        # Store all five dimension scores plus detail dicts in score_breakdown.
        # The Sheets writer and CLI both use this for transparency.
        return ScoredJob(
            **job.model_dump(),
            score=round(total, 1),
            score_breakdown={
                "title":        round(title_pts, 1),
                "tech_stack":   round(tech_pts, 1),
                "location":     round(loc_pts, 1),
                "work_type":    round(work_pts, 1),
                "requirements": round(req_pts, 1),
                # Embed per-dimension details so the breakdown is self-documenting
                **title_info,
                **loc_info,
                **work_info,
                **req_info,
            },
            work_type=work_info["work_arrangement"],
            tech_stack_found=tech_info["matched"],
        )

    # ── Scoring dimensions ─────────────────────────────────────────────────────

    def _score_title(self, job_title: str) -> tuple[float, dict]:
        """
        Score: 0–25 points.

        Compares the job title against each of the user's target titles using
        rapidfuzz's token_set_ratio, which:
          - Ignores word order ("Data Scientist Senior" == "Senior Data Scientist")
          - Handles partial overlaps ("ML Engineer" scores highly vs "machine learning engineer")
          - Is case-insensitive (handled by the .lower() calls below)

        We take the BEST match across all target titles so the user doesn't need to
        list every possible title variation.
        """
        best_score = 0
        best_match = ""
        for target in self._profile.target_titles:
            score = fuzz.token_sort_ratio(job_title.lower(), target.lower())
            if score > best_score:
                best_score = score
                best_match = target

        title_lower = job_title.lower()
        has_keyword = any(kw in title_lower for kw in self._required_keywords)

        if not has_keyword and self._strict:
            pts = 0
        elif best_score < self._title_threshold:
            pts = 0
        else:
            pts = best_score / 100.0 * _W_TITLE

        return pts, {"title_best_match": best_match, "title_ratio": best_score, "has_required_keyword": has_keyword}

    def _score_tech(self, description_html: str) -> tuple[float, dict]:
        """
        Score: 0–30 points.

        Counts how many of the user's skills appear in the job description text.
        Uses the alias table from tech_keywords.yaml so "PyTorch" matches even if
        the description says "torch" (the common import alias).

        Scoring formula: matched_count / tech_full_match_count * 30, capped at 30.
        tech_full_match_count is set in settings.yaml (default 6, roughly half a
        typical skill set). A job listing won't name every skill, but reaching the
        threshold is a strong compatibility signal.
        """
        text = _strip_html(description_html).lower()
        matched: list[str] = []

        for skill, aliases in self._skill_aliases.items():
            # Any alias appearing anywhere in the description counts as a match
            if any(alias in text for alias in aliases):
                matched.append(skill)

        ratio = len(matched) / self._tech_full_match_count
        pts   = min(ratio * _W_TECH, float(_W_TECH))
        return pts, {"matched": matched}

    def _score_location(self, location_raw: Optional[str]) -> tuple[float, dict]:
        """
        Score: 0–20 points.

        Awards points based on the location tier from config/locations.yaml:
          Tier 1 (Tel Aviv metro): 20 pts  ← maximum
          Tier 2 (Modi'in area):   15 pts
          Tier 3 (Jerusalem):      12 pts
          Ambiguous ("Israel"):    10 pts  ← benefit of the doubt
          Unknown (no location):   10 pts

        Cities with an Israel Railways station receive a +3 bonus, capped at 20.
        The bonus has no visible effect for tier-1 cities (already at the cap)
        but is meaningful for tier-2 (Modi'in → 18) and tier-3 (Jerusalem → 15).
        """
        if not location_raw:
            return float(_UNKNOWN_LOCATION_SCORE), {
                "area": "unknown", "tier": None, "train_bonus": 0
            }

        # Translate non english city names to English before matching
        loc = location_raw
        for other, eng in self._non_english.items():
            loc = loc.replace(other, eng)
        loc_lower = loc.lower()

        # "Israel" alone — too vague to score precisely, give neutral points
        if re.match(r"^\s*(israel|il|ישראל)\s*$", loc_lower, re.IGNORECASE):
            return float(_AMBIGUOUS_LOCATION_SCORE), {
                "area": "ambiguous", "tier": None, "train_bonus": 0
            }

        # Substring city lookup — finds "herzliya" inside "Herzliya, Israel"
        matched_area = None
        matched_city = None
        for city_lower, area_name in self._city_to_area.items():
            if city_lower in loc_lower:
                matched_area = area_name
                matched_city = city_lower
                break

        if matched_area is None:
            # Location was out of target areas; the Disqualifier should have caught this.
            # Return 0 gracefully instead of raising so tests can score any job directly.
            return 0.0, {"area": "out_of_area", "tier": None, "train_bonus": 0}

        tier = self._areas.get(matched_area, {}).get("tier", 3)
        base_pts = _TIER_SCORES.get(tier, _AMBIGUOUS_LOCATION_SCORE)

        # Train bonus: awarded when the matched city has a direct train station
        has_train = (matched_city in self._train_cities) if matched_city else False
        train_bonus = _TRAIN_BONUS if has_train else 0

        # Cap at the maximum location score
        pts = min(base_pts + train_bonus, float(_W_LOCATION))
        return pts, {"area": matched_area, "tier": tier, "train_bonus": train_bonus}

    def _score_work_type(self, job: RawJob) -> tuple[float, dict]:
        """
        Score: 6–10 points.

        Detects the working arrangement from the location field and description.
        Hybrid is the user's stated preference (10 pts); on-site is acceptable (8 pts);
        unknown gets a small neutral score rather than zero (6 pts).

        Fully remote jobs are already filtered by the Disqualifier so they never
        appear here — there is no 0-point case in practice.
        """
        combined = (job.location_raw or "") + " " + job.description_html

        # Return "work_arrangement" (not "work_type") to avoid a key collision when this
        # dict is spread into score_breakdown alongside the numeric "work_type" entry.
        if _HYBRID_RE.search(combined):
            return float(_WORK_HYBRID_SCORE), {"work_arrangement": "hybrid"}
        if _ONSITE_RE.search(combined):
            return float(_WORK_ONSITE_SCORE), {"work_arrangement": "on-site"}
        return float(_WORK_UNKNOWN_SCORE), {"work_arrangement": "unknown"}

    def _score_requirements(self, description_html: str) -> tuple[float, dict]:
        """
        Score: 3–15 points.

        Extracts years-of-experience requirements from the description text and
        compares the maximum stated requirement to the user's actual experience.

        Uses a permissive regex (_YEARS_EXP_RE) because a false soft-penalty (scoring
        a good job a bit lower) is less harmful than missing the signal entirely.
        Hard disqualification (>7 years) is handled separately by the Disqualifier.

        Scoring ladder (user has 3 years):
          gap ≤ 0 (requires ≤ 3 years)  → 15 pts  (meets or exceeds requirement)
          gap = 1 (requires 4 years)     → 12 pts  (one year short — competitive)
          gap = 2 (requires 5 years)     →  8 pts  (two years short — stretch)
          gap ≥ 3 (requires 6–7 years)   →  3 pts  (significant gap — unlikely)
          no requirement stated          → 10 pts  (neutral — unknown alignment)
        """
        text = _strip_html(description_html).lower()
        user_years = self._profile.years_of_experience

        # Collect all "N years of experience" values; take the most demanding one
        years_found: list[int] = []
        for m in _YEARS_EXP_RE.finditer(text):
            y = int(m.group(1))
            if 1 <= y <= 30:  # filter out calendar years (2024) and zeros
                years_found.append(y)

        if not years_found:
            return float(_REQ_NO_MENTION), {
                "years_required": None, "user_years": user_years
            }

        years_required = max(years_found)
        gap = years_required - user_years  # positive → user is under-experienced

        if gap <= 0:
            pts = _REQ_EXACT_MATCH
        elif gap == 1:
            pts = _REQ_ONE_SHORT
        elif gap == 2:
            pts = _REQ_TWO_SHORT
        else:
            pts = _REQ_FAR_SHORT

        return float(pts), {"years_required": years_required, "user_years": user_years}


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Score qualified jobs against a user profile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", required=True, help="JSON array of RawJob objects")
    parser.add_argument(
        "--profile",
        default="tests/fixtures/user_profile.json",
        help="UserProfile JSON file",
    )
    parser.add_argument(
        "--tech-keywords",
        default="config/tech_keywords.yaml",
        help="tech_keywords.yaml path",
    )
    parser.add_argument(
        "--locations",
        default="config/locations.yaml",
        help="locations.yaml path",
    )
    parser.add_argument("--output", help="Write ScoredJob JSON here (default: stdout)")
    parser.add_argument(
        "--min-score", type=float, default=0.0, help="Only output jobs at or above this score"
    )
    parser.add_argument(
        "--settings",
        default="config/settings.yaml",
        help="settings.yaml path (for scoring.* config values)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    import yaml

    with open(args.locations, encoding="utf-8") as f:
        locations = yaml.safe_load(f)
    with open(args.tech_keywords, encoding="utf-8") as f:
        tech_keywords = yaml.safe_load(f) or {}

    scoring_config: dict = {}
    try:
        with open(args.settings, encoding="utf-8") as f:
            scoring_config = (yaml.safe_load(f) or {}).get("scoring", {})
    except FileNotFoundError:
        pass  # settings file is optional for the CLI; use module defaults

    profile = UserProfile.from_json(args.profile)

    with open(args.input) as f:
        raw = json.load(f)

    jobs    = [RawJob(**j) for j in raw]
    scorer  = Scorer(profile, locations, tech_keywords, scoring_config=scoring_config)
    scored  = [scorer.score(j) for j in jobs]
    scored.sort(key=lambda j: j.score, reverse=True)

    passing = [j for j in scored if j.score >= args.min_score]

    if args.verbose:
        for job in scored:
            flag = "✓" if job.score >= args.min_score else "✗"
            bd = job.score_breakdown
            print(
                f"  {flag} [{job.score:5.1f}] {job.title} @ {job.location_raw}"
                f" | title={bd.get('title', 0):.0f}"
                f" tech={bd.get('tech_stack', 0):.0f}"
                f" loc={bd.get('location', 0):.0f}"
                f" work={bd.get('work_type', 0):.0f}"
                f" req={bd.get('requirements', 0):.0f}",
                file=sys.stderr,
            )
        print(
            f"\n{len(passing)}/{len(scored)} jobs above min-score {args.min_score}",
            file=sys.stderr,
        )

    output_data = json.dumps(
        [j.model_dump() for j in passing], indent=2, default=str
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_data)
        print(f"Wrote {len(passing)} scored jobs to {args.output}", file=sys.stderr)
    else:
        print(output_data)


if __name__ == "__main__":
    main()
