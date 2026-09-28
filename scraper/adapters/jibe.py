"""iCIMS "Jibe" career-site adapter.

Many companies on iCIMS front it with a branded career site (careers.amd.com,
careers.jhuapl.edu, careers.garmin.com, jobs.keysight.com) whose frontend
reads a public JSON API on the same host:

    GET https://{host}/api/jobs?page=1&limit=100&sortBy=posted_date&descending=true

Recognise these sites by apply links ending in ``?icims=1``. The job page is
https://{host}/jobs/{slug}.

The tempting wrong door is the company's native iCIMS portal
(careers-{company}.icims.com): it serves HTML only, with no JSON API. Results
are newest-first, so like Workday we stop after MAX_POSTINGS.
"""

import html
import re

import requests

from scraper.models import Job

API_URL = "https://{host}/api/jobs"
JOB_URL = "https://{host}/jobs/{slug}"
PAGE_SIZE = 100
MAX_POSTINGS = 200
DESCRIPTION_LIMIT = 500
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    host = config["host"]
    jobs: list[Job] = []
    for page in range(1, MAX_POSTINGS // PAGE_SIZE + 1):
        response = requests.get(
            API_URL.format(host=host),
            params={"page": page, "limit": PAGE_SIZE, "sortBy": "posted_date",
                    "descending": "true"},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        postings = [entry.get("data") or {} for entry in payload.get("jobs", [])]
        jobs.extend(_to_job(posting, company, host) for posting in postings)
        if len(postings) < PAGE_SIZE or len(jobs) >= payload.get("totalCount", 0):
            break
    return jobs


def _to_job(posting: dict, company: str, host: str) -> Job:
    slug = posting.get("slug") or posting.get("req_id")
    return Job(
        id=f"jibe:{host}:{slug}",
        title=posting.get("title", ""),
        company=company,
        location=posting.get("full_location") or posting.get("short_location") or "",
        url=JOB_URL.format(host=host, slug=slug),
        posted_at=_iso(posting.get("posted_date")),
        description=_description(posting.get("description") or ""),
        source=f"jibe/{company}",
    )


def _iso(posted: str | None) -> str | None:
    # "2026-09-27T16:36:00+0000": fromisoformat wants the offset as +00:00.
    if not posted:
        return None
    return re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", posted)


def _description(content: str) -> str:
    # Like Greenhouse, the description is HTML-escaped HTML: unescape to get
    # markup, strip the tags, then unescape entities that sat inside it.
    text = re.sub(r"<[^>]*(?:>|$)", " ", html.unescape(content))  # incl. a cut-off tag
    return " ".join(html.unescape(text).split())[:DESCRIPTION_LIMIT]
