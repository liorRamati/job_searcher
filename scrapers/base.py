from abc import ABC, abstractmethod
from models.job import RawJob, CompanyConfig


def build_search_query(job_titles: list[str]) -> str:
    """
    Build a boolean OR search query from a list of job titles.

    Returns an empty string when the list is empty (meaning "search everything").
    Most job board search APIs (Eightfold, Amazon, Google, Meta, etc.) support
    boolean OR in their keyword/query field.

    Example:
        ["data scientist", "ML engineer"] → "data scientist OR ML engineer"
    """
    return " OR ".join(job_titles) if job_titles else ""


class BaseScraper(ABC):
    @abstractmethod
    def fetch_jobs(self, company: CompanyConfig, max_age_days: int = 30) -> list[RawJob]:
        """Fetch and normalize job listings for a single company."""
        pass
