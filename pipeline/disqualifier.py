"""
Job disqualifier — hard rules that reject a job before it reaches the scorer.

Universal rules (location + remote) run for all jobs regardless of profile.
Profile-based rules (excluded titles, excluded tech keywords, seniority cap) run
only when a UserProfile is provided.

Rule order in check(): universal rules first (cheapest), profile rules last.
Short-circuits on the first match so the returned reason is always the most
fundamental problem with the job.

Disqualified jobs are not written to Google Sheets and are not scored.
When in doubt, don't disqualify — let the scorer penalize instead.

Standalone usage:
    python -m pipeline.disqualifier --input jobs.json --output qualified.json --verbose
    python -m pipeline.disqualifier --input jobs.json --disqualified-output dq.json --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Optional

import yaml
from bs4 import BeautifulSoup

from models.job import DisqualifyResult, RawJob


# ── Remote / hybrid detection patterns ────────────────────────────────────────

# Signals that a position is FULLY remote (no office presence).
_REMOTE_LOCATION_PATTERNS = re.compile(
    r"\bremote\b|\bwork from home\b|\bwfh\b", re.IGNORECASE
)
_FULLY_REMOTE_DESC_PATTERNS = re.compile(
    r"fully remote|100% remote|entirely remote|work from home only", re.IGNORECASE
)

# Signals that override a "remote" signal — the job is hybrid, not fully remote.
# If these appear alongside "remote", the job is NOT disqualified.
_HYBRID_SIGNALS = re.compile(
    r"\bhybrid\b|flexible work|partial remote|partially remote", re.IGNORECASE
)

# ── Location patterns ──────────────────────────────────────────────────────────

# Matches location strings that are too vague to disqualify on.
# e.g. "Israel", "IL", "ישראל" — we give the benefit of the doubt.
_AMBIGUOUS_ISRAEL = re.compile(r"^\s*(israel|il|ישראל)\s*$", re.IGNORECASE)

# ── Seniority / years-of-experience patterns ──────────────────────────────────

# Conservative regex for the hard-disqualifier seniority check.
# We intentionally require strong evidence (explicit "+", a range, or a context word)
# to avoid false positives like "our company has 15 years of experience".
#
# Matches:
#   "10+ years experience"            ← "+" makes the candidate requirement unambiguous
#   "5-10 years of experience"        ← range format
#   "minimum 8 years"                 ← context word
#   "requires 9 years of exp"         ← context word
_SENIORITY_HARD_RE = re.compile(
    r"(\d+)\+\s*years?\s*(?:of\s+)?(?:experience|exp)\b"
    r"|(\d+)\s*[-–]\s*\d+\s*years?\s*(?:of\s+)?(?:experience|exp)\b"
    r"|(?:minimum|at least|require[sd]?|must have|need[sd]?)\s+(\d+)\s*\+?\s*years?",
    re.IGNORECASE,
)


def _strip_html(html: str) -> str:
    """Return plain text with HTML tags removed. Used before applying text regexes."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ")


def _extract_years_hard(text: str) -> Optional[int]:
    """
    Extract the maximum years-of-experience requirement from job description text.
    Returns None if no clear requirement is found.

    Uses _SENIORITY_HARD_RE which requires strong linguistic evidence ("+", a range,
    or a context word like "minimum") to avoid false disqualifications.
    """
    years: list[int] = []
    for m in _SENIORITY_HARD_RE.finditer(text):
        # The regex has three capturing groups (one per alternative); only one will fire.
        raw = next((g for g in m.groups() if g is not None), None)
        if raw is None:
            continue
        y = int(raw)
        # Sanity-filter: realistic years-of-experience values only.
        # Avoids matching calendar years (e.g. "2024") or absurd numbers.
        if 1 <= y <= 30:
            years.append(y)
    return max(years) if years else None


class Disqualifier:
    """
    Applies hard rules to a RawJob and returns a DisqualifyResult.

    Universal rules (location + remote) always run.
    Profile-based rules run only when a UserProfile is passed to __init__.
    The rules run in order and short-circuit on the first match so the returned
    reason is always the most fundamental problem with the job.
    """

    def __init__(self, locations: dict, profile=None, disqualify_remote: bool = True):
        """
        Parameters
        ----------
        locations : parsed config/locations.yaml. Pass an empty dict to skip location checks
                    (used in LLM mode, where the LLM handles location filtering instead).
        profile   : optional UserProfile — enables profile-based hard disqualifiers
                    (excluded titles, excluded tech keywords, over-seniority).
                    Pass None to run universal rules only.
        disqualify_remote: boolean indicating whether to disqualify remote jobs.
        """
        self._areas: dict = locations.get("areas", {})
        self._non_english: dict = locations.get("non_english_city_names", {})
        self._profile = profile  # None → universal rules only (no profile-based checks)
        self._disqualify_remote = disqualify_remote

        # Build flat lowercase city → area-name lookup used by _find_area().
        # Stored as lowercase so comparison is a simple "key in dict" check.
        self._city_to_area: dict[str, str] = {}
        for area_name, area_data in self._areas.items():
            for city in area_data.get("cities", []):
                self._city_to_area[city.lower()] = area_name

    # ── Public ────────────────────────────────────────────────────────────────

    def check(self, job: RawJob) -> DisqualifyResult:
        """
        Run all applicable disqualification checks, returning on the first match.

        Rule order matters: location/remote checks run first (cheapest),
        profile-based checks run last (require HTML parsing).
        """
        result = self._check_remote(job)
        if result and self._disqualify_remote:
            return result
        # Skip location check when no city map was built (e.g. in LLM filter mode)
        if self._city_to_area:
            if result := self._check_location(job):
                return result

        # Profile-based rules — skipped when no profile was provided.
        # Order: cheapest first (title-only), then body-parsing checks.
        if self._profile is not None:
            if result := self._check_excluded_titles(job):
                return result
            if result := self._check_excluded_title_keywords(job):
                return result
            if result := self._check_excluded_keywords(job):
                return result
            if result := self._check_dominant_tech_stack(job):
                return result
            if result := self._check_seniority_hard(job):
                return result

        return DisqualifyResult(is_disqualified=False)

    # ── Universal rules: remote / location ────────────────────────────────────

    def _check_remote(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if the job is fully remote (no office presence).

        Two signals are checked:
          1. The location field explicitly says "remote" / "WFH".
          2. The description body says "fully remote" / "100% remote".

        In both cases, a hybrid qualifier (e.g. "hybrid", "flexible work") overrides
        the remote signal — those jobs require office presence and are not filtered.
        """
        location = job.location_raw or ""
        desc = job.description_html

        # Location field says "remote" but there's no hybrid qualifier
        if _REMOTE_LOCATION_PATTERNS.search(location):
            if not _HYBRID_SIGNALS.search(location):
                return DisqualifyResult(
                    is_disqualified=True,
                    reason="Remote-only position (location field)",
                )

        # Description uses strong "fully remote" language without a hybrid qualifier
        if _FULLY_REMOTE_DESC_PATTERNS.search(desc):
            if not _HYBRID_SIGNALS.search(desc):
                return DisqualifyResult(
                    is_disqualified=True,
                    reason="Remote-only position (description)",
                )

        return None

    def _check_location(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if the job's location is outside all target areas.

        Returns None (passes) when:
          - A known target city is found in the location string.
          - The location is ambiguous (e.g. "Israel" or blank) — benefit of the doubt.

        Returns a DisqualifyResult when a specific, non-target city is identified.
        """
        area = self._find_area(job.location_raw)
        if area is None:
            # _find_area returns None only when a specific location was given
            # that doesn't match any target city.
            display = job.location_raw or "(no location specified)"
            return DisqualifyResult(
                is_disqualified=True,
                reason=f"Location not in target areas: {display}",
            )
        return None

    # ── Profile-based hard disqualifiers ──────────────────────────────────────

    def _check_excluded_titles(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if the job title indicates a seniority or role mismatch.

        Uses word-boundary matching so short patterns like "VP" don't fire on "MVP",
        and "Lead" doesn't fire if it appears only in "Leadership skills required".
        Only the job title is searched — we don't want "Manager" in a job description
        body to rule out an otherwise great IC (individual contributor) role.
        """
        title_lower = job.title.lower()
        for excluded in self._profile.hard_disqualifiers.excluded_titles:
            pattern = r"\b" + re.escape(excluded.lower()) + r"\b"
            if re.search(pattern, title_lower, re.IGNORECASE):
                return DisqualifyResult(
                    is_disqualified=True,
                    reason=f"Title contains excluded role: {excluded}",
                )
        return None

    def _check_excluded_title_keywords(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if the job title contains a word that indicates the role is
        centred on a tech stack or role type the user doesn't want.

        This is the simplest and cheapest check: a job titled "Frontend Engineer"
        or "Full-Stack Developer" is unambiguously the wrong kind of role without
        needing to look at the description at all.

        Uses plain case-insensitive substring matching (no word boundary needed)
        because these keywords — "frontend", "full-stack", "ui developer" — are
        specific enough that false positives in real job titles are negligible.

        Unlike excluded_titles (Manager, VP — seniority/role level), these target
        the technology direction of the role.

        Whitespace around hyphens is normalized before matching so that ATS
        formatting quirks like "Full- Stack" (space before hyphen) still match
        the keyword "full-stack".
        """
        # Collapse any whitespace around hyphens: "Full- Stack" → "full-stack"
        title_lower = re.sub(r"\s*-\s*", "-", job.title.lower())
        for keyword in self._profile.hard_disqualifiers.excluded_title_keywords:
            if keyword.lower() in title_lower:
                return DisqualifyResult(
                    is_disqualified=True,
                    reason=f"Title indicates excluded tech role: '{keyword}'",
                )

        role_keywords = getattr(
            self._profile.hard_disqualifiers, "excluded_title_keywords_role", []
        )
        if role_keywords:
            for keyword in role_keywords:
                if keyword.lower() in title_lower:
                    return DisqualifyResult(
                        is_disqualified=True,
                        reason=f"Title contains excluded role type: '{keyword}'",
                    )

        return None

    def _check_dominant_tech_stack(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if a technology stack is clearly the *primary focus* of the job.

        Unlike excluded_keywords (any mention = out), this rule tolerates passing
        references — "some C++ knowledge is a bonus" — and only fires when the
        technology dominates the requirements. This is important because ML/data
        jobs sometimes touch C++ for performance-critical code or mention JavaScript
        for dashboards without that being the core of the work.

        Two independent signals, either one is sufficient:
          1. Title signal: any of the stack's title_keywords appears in the job title.
             This is cheap and highly reliable — "React Developer" leaves no doubt.
          2. Body dominance: total occurrences of all body_keywords in the description
             reaches body_threshold. A job that says "React, TypeScript, CSS, HTML,
             Angular" is clearly a frontend job even if the title is generic.

        The body count is a SUM across all keywords so that a description saying
        "react (x3), typescript (x2)" accumulates to 5 mentions — not just one
        keyword appearing multiple times.
        """
        # Same hyphen normalization as _check_excluded_title_keywords
        title_lower = re.sub(r"\s*-\s*", "-", job.title.lower())
        body = _strip_html(job.description_html).lower()

        for stack in self._profile.hard_disqualifiers.dominant_tech_stacks:
            # Title check: even one matching keyword is decisive
            for kw in stack.title_keywords:
                if kw.lower() in title_lower:
                    return DisqualifyResult(
                        is_disqualified=True,
                        reason=f"Title indicates {stack.name} focus: '{kw}'",
                    )

            # Body dominance: sum occurrences across every body keyword
            total_mentions = sum(body.count(kw.lower()) for kw in stack.body_keywords)
            if total_mentions >= stack.body_threshold:
                return DisqualifyResult(
                    is_disqualified=True,
                    reason=(
                        f"Description focuses on {stack.name} "
                        f"({total_mentions} mentions ≥ threshold {stack.body_threshold})"
                    ),
                )

        return None

    def _check_excluded_keywords(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if the job requires a technology the user has excluded.

        Searches both the job title and the full description so we catch cases where
        the excluded tech appears in the requirements even if the title looks fine.

        Matching strategy:
          - Keywords with non-word characters (e.g. "C++") use simple substring match
            because regex word boundaries don't work reliably next to symbols.
          - Pure-word keywords (e.g. "PHP", "Ruby") use word-boundary matching to
            avoid false positives like "PHPBB" or "ruby" as a colour adjective.
        """
        # Combine title + plain description text for a single search pass
        text = (job.title + " " + _strip_html(job.description_html)).lower()

        for keyword in self._profile.hard_disqualifiers.excluded_keywords:
            k = keyword.lower()
            # Detect whether the keyword contains special regex characters (like "+")
            has_special_chars = bool(re.search(r"\W", keyword))

            if has_special_chars:
                # Simple substring search — safe for "C++", "C#", etc.
                matched = k in text
            else:
                # Word boundary search — avoids "PHP" matching inside "PHPBB"
                matched = bool(re.search(r"\b" + re.escape(k) + r"\b", text))

            if matched:
                return DisqualifyResult(
                    is_disqualified=True,
                    reason=f"Requires excluded skill: {keyword}",
                )
        return None

    def _check_seniority_hard(self, job: RawJob) -> Optional[DisqualifyResult]:
        """
        Disqualify if the job's stated years-of-experience requirement exceeds the
        user's configured maximum (hard_disqualifiers.min_years_required_max).

        Uses a conservative regex (_SENIORITY_HARD_RE) that requires explicit "+",
        a numeric range, or a context word (minimum / requires / must have).
        This avoids false positives from sentences like
        "our company has 15 years of experience building X".

        Note: This check fires at > threshold, not >=.
        A job requiring exactly the threshold (e.g. 7 years) is borderline but not
        automatically out — the scorer will penalise it appropriately.
        """
        text = _strip_html(job.description_html).lower()
        max_years = self._profile.hard_disqualifiers.min_years_required_max

        years_required = _extract_years_hard(text)
        if years_required is not None and years_required > max_years:
            return DisqualifyResult(
                is_disqualified=True,
                reason=(
                    f"Requires {years_required}+ years of experience "
                    f"(user's limit: {max_years})"
                ),
            )
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _translate_to_english(self, text: str) -> str:
        """Replace non english city names with their English equivalents."""
        for heb, eng in self._non_english.items():
            text = text.replace(heb, eng)
        return text

    def _find_area(self, location_raw: Optional[str]) -> Optional[str]:
        """
        Classify a raw location string into an area name, 'ambiguous', 'unknown',
        or None (= out of target areas → should be disqualified).

        Return values:
          area_name  : matched a target city (e.g. 'tel_aviv', 'modiin')
          'ambiguous': location is just "Israel" or equivalent — don't disqualify
          'unknown'  : no location provided — benefit of the doubt, don't disqualify
          None       : a specific location was given that doesn't match any target city
        """
        if not location_raw:
            # Missing location — we can't know if it's local or not, so pass it.
            return "unknown"

        # Translate any non english city names to English before pattern matching
        loc = self._translate_to_english(location_raw).lower()

        # "Israel" / "IL" alone is too vague to disqualify
        if _AMBIGUOUS_ISRAEL.match(loc):
            return "ambiguous"

        # Substring search: check whether any known target city appears in the string.
        # This handles formats like "Tel Aviv, Israel" and "Herzliya" equally.
        for city_lower, area_name in self._city_to_area.items():
            if city_lower in loc:
                return area_name

        # No target city found — location is specific but not in our target areas
        return None


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Disqualify jobs that fail hard rules (universal: location + remote; profile-based: titles, keywords, seniority)",
        epilog=__doc__,
    )
    parser.add_argument("--input", required=True, help="JSON array of RawJob objects")
    parser.add_argument("--profile", help="UserProfile JSON (enables profile-based rules)")
    parser.add_argument("--output", help="File for qualified jobs (default: stdout)")
    parser.add_argument("--disqualified-output", help="File for disqualified jobs (optional)")
    parser.add_argument(
        "--locations", default="config/locations.yaml", help="Path to locations.yaml"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    with open(args.locations) as f:
        locations = yaml.safe_load(f)

    # Load profile if provided (enables profile-based hard disqualifiers)
    profile = None
    if args.profile:
        from models.profile import UserProfile
        profile = UserProfile.from_json(args.profile)

    with open(args.input) as f:
        raw = json.load(f)

    jobs = [RawJob(**j) for j in raw]
    disqualifier = Disqualifier(locations, profile)

    qualified: list[dict] = []
    disqualified: list[dict] = []

    for job in jobs:
        result = disqualifier.check(job)
        if result.is_disqualified:
            entry = job.model_dump()
            entry["_disqualify_reason"] = result.reason
            disqualified.append(entry)
            if args.verbose:
                print(f"  SKIP [{result.reason}]: {job.title} @ {job.location_raw}", file=sys.stderr)
        else:
            qualified.append(job.model_dump())

    if args.verbose:
        print(
            f"\n{len(qualified)} qualified, {len(disqualified)} disqualified "
            f"(from {len(jobs)} total)",
            file=sys.stderr,
        )

    output_data = json.dumps(qualified, indent=2, default=str)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output_data)
    else:
        print(output_data)

    if args.disqualified_output:
        with open(args.disqualified_output, "w") as f:
            json.dump(disqualified, f, indent=2, default=str)


if __name__ == "__main__":
    main()
