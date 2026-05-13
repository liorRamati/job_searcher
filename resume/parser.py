"""
Resume parser — extracts user profile from PDF/Word resumes using LLM.

Usage:
    python -m resume.parser --input inputs/resume/resume.pdf --output tests/fixtures/user_profile.json

The parser:
1. Extracts text from PDF or Word files
2. Sends the text to an LLM to extract profile information
3. Writes the profile to a JSON file
4. Caches the result based on file hash - only re-runs when resume changes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

_log = logging.getLogger("job_searcher.resume_parser")


RESUME_SYSTEM_PROMPT = """You are a resume parser. Extract structured information from the provided resume text.
Output a JSON object with the following fields:

{
  "name": "Full name",
  "email": "email@example.com",
  "phone": "phone number",
  "seniority": "junior" | "mid" | "senior",
  "years_of_experience": number,
  "target_titles": ["list of job titles the person is looking for"],
  "skills": {
    "languages": ["Python", "SQL", ...],
    "frameworks": ["PyTorch", "Pandas", ...],
    "tools": ["Git", "Docker", ...],
    "domains": ["machine learning", "data science", ...]
  },
  "education": [list of the format "{
    "degree": "B.Sc./M.Sc./...",
    "field": "Computer Science/mathematics/physics/...",
    "institution": "University name",
    "year": YYYY
  }"],
  "previous_titles": ["list of titles of previous jobs"],
  "languages": ["Hebrew", "English", ...],
}

Extract as much information as possible from the resume. If a field cannot be determined, use reasonable defaults based on the content.
For target_titles, infer from the person's experience and skills what jobs they would be qualified for.
For seniority, estimate based on years of experience: 0-2 = junior, 3-5 = mid, 6+ = senior.
Output ONLY the JSON, no additional text."""


def extract_text_from_file(file_path: str) -> str:
    """
    Extract text from PDF or Word files.

    Args:
        file_path: Path to the resume file.

    Returns:
        Extracted text content.

    Raises:
        ValueError: If file type is not supported.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return extract_from_pdf(file_path)
    elif suffix in [".docx", ".doc"]:
        return extract_from_docx(file_path)
    else:
        raise ValueError(f"Unsupported file type: {suffix}. Use PDF or Word (.docx)")


def extract_from_pdf(file_path: str) -> str:
    """Extract text from PDF using pdfplumber."""
    import pdfplumber

    text_parts = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def extract_from_docx(file_path: str) -> str:
    """Extract text from Word documents using python-docx."""
    from docx import Document

    doc = Document(file_path)
    text_parts = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            text_parts.append(paragraph.text)
    return "\n\n".join(text_parts)


def get_file_hash(file_path: str) -> str:
    """Calculate MD5 hash of a file for caching."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def _create_hard_disqualifiers(disqualifiers_file_path: str) -> dict:
    try:
        with open(disqualifiers_file_path, "r") as f:
            disqualifiers = yaml.safe_load(f)
    except FileNotFoundError:
        return {}
    return disqualifiers


def parse_resume(
    resume_path: str,
    llm_client,
    disqualifiers_file_path: str = "../config/disqualifiers.yaml",
    output_path: str = "../tests/fixtures/user_profile.json",
) -> dict:
    """
    Parse a resume file and extract user profile using LLM.

    Args:
        resume_path: Path to the resume file (PDF or Word).
        llm_client: An LLM client instance (BaseLLM).
        disqualifiers_file_path: Path to disqualifiers file.
        output_path: Path to write the profile JSON (used for caching).

    Returns:
        Parsed user profile as a dict.
    """
    path = Path(resume_path)

    if not path.exists():
        raise FileNotFoundError(f"Resume file not found: {resume_path}")

    output_path_obj = Path(output_path)
    output_path_obj.parent.mkdir(parents=True, exist_ok=True)

    _log.info(f"Extracting text from {resume_path}...")
    text = extract_text_from_file(resume_path)
    disqualifiers = _create_hard_disqualifiers(disqualifiers_file_path)

    if not text.strip():
        raise ValueError(f"No text could be extracted from {resume_path}")

    _log.info("Sending resume text to LLM for parsing...")
    try:
        response = llm_client.complete(
            prompt=text,
            system_message=RESUME_SYSTEM_PROMPT,
            max_tokens=2000,
            temperature=0.3,
        )
    except Exception as exc:
        _log.error(f"LLM call failed during resume parsing: {exc}", exc_info=True)
        raise

    response = response.strip()
    if response.startswith("```json"):
        response = response[7:]
    if response.startswith("```"):
        response = response[3:]
    if response.endswith("```"):
        response = response[:-3]
    response = response.strip()

    try:
        profile = json.loads(response)
    except json.JSONDecodeError as e:
        _log.error(
            f"Failed to parse LLM response as JSON: {e}. "
            f"Raw response (first 500 chars): {response[:500]!r}",
            exc_info=True,
        )
        raise

    profile["hard_disqualifiers"] = disqualifiers

    with open(output_path_obj, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    _log.info(f"Profile written to {output_path}")

    return profile


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point for the resume parser."""
    parser = argparse.ArgumentParser(description="Parse resume and extract user profile")
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the resume file (PDF or Word)",
    )
    parser.add_argument(
        "--output",
        default="../tests/fixtures/user_profile.json",
        help="Output path for the JSON profile",
    )
    parser.add_argument(
        "--config",
        default="../config/settings.yaml",
        help="Path to settings.yaml for LLM config",
    )
    parser.add_argument(
        "--disqualifiers_file",
        default="../config/disqualifiers.yaml",
        help="Path to disqualifiers.yaml for LLM config",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable cache and re-parse even if cached",
    )

    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    from llm import create_llm

    llm_config = config.get("llm", {})
    llm_client = create_llm(llm_config)
    print(f"Using LLM provider: {llm_config.get('provider')}")

    try:
        parse_resume(args.input, llm_client, args.disqualifiers_file, args.output)
    except Exception as e:
        print(f"Error parsing resume: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()