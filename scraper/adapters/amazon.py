"""Amazon careers adapter (amazon.jobs).

Amazon runs its own careers site. Its search page reads a public JSON
endpoint:

    GET https://www.amazon.jobs/en/search.json?result_limit=100&offset=0&sort=recent
        &normalized_country_code[]=USA&normalized_country_code[]=CAN

Without a country filter the board is worldwide (10k+ hits, mostly
warehouse and non-US roles), so ``countries`` takes ISO-3 codes and
defaults to USA + CAN. ``sort=recent`` makes results newest-first, so like
Workday we stop after MAX_POSTINGS. The job page is
https://www.amazon.jobs{job_path}.

``company_name`` names the hiring subsidiary ("Annapurna Labs Ltd.",
"Amazon Web Services, Inc."); every job is filed under ``company`` so
dedup, stats and season alerts see one employer.
"""

import html
import re
from datetime import UTC, datetime

import requests

from scraper.models import Job

API_URL = "https://www.amazon.jobs/en/search.json"
SITE_URL = "https://www.amazon.jobs"
PAGE_SIZE = 100
MAX_POSTINGS = 200
DESCRIPTION_LIMIT = 500
TIMEOUT_SECONDS = 30
DEFAULT_COUNTRIES = ("USA", "CAN")


def fetch(config: dict) -> list[Job]:
    company = config.get("company", "amazon")
    countries = list(config.get("countries") or DEFAULT_COUNTRIES)
    jobs: list[Job] = []
    for offset in range(0, MAX_POSTINGS, PAGE_SIZE):
        response = requests.get(
            API_URL,
            params={"result_limit": PAGE_SIZE, "offset": offset, "sort": "recent",
                    "normalized_country_code[]": countries},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        postings = payload.get("jobs") or []
        jobs.extend(_to_job(posting, company) for posting in postings)
        if len(postings) < PAGE_SIZE or len(jobs) >= payload.get("hits", 0):
            break
    return jobs


def _to_job(posting: dict, company: str) -> Job:
    return Job(
        id=f"amazon:{company}:{posting['id_icims']}",
        title=" ".join((posting.get("title") or "").split()),
        company=company,
        location=posting.get("normalized_location") or posting.get("location") or "",
        url=f"{SITE_URL}{posting.get('job_path', '')}",
        posted_at=_iso(posting.get("posted_date")),
        description=_description(posting.get("description_short")
                                 or posting.get("description") or ""),
        source=f"amazon/{company}",
    )


def _iso(posted: str | None) -> str | None:
    # "September 27, 2026"
    if not posted:
        return None
    try:
        return datetime.strptime(posted, "%B %d, %Y").replace(tzinfo=UTC).date().isoformat()
    except ValueError:
        return None


def _description(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", markup)
    return " ".join(html.unescape(text).split())[:DESCRIPTION_LIMIT]
