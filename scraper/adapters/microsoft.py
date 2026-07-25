"""Microsoft careers adapter.

Microsoft runs a custom careers site (jobs.careers.microsoft.com) with no
standard ATS board API, but its search backend is a public, unauthenticated
JSON endpoint - the same one the site's own frontend calls:

    GET https://gcsservices.careers.microsoft.com/search/api/v1/search
        ?q=&l=en_us&pg=1&pgSz=20&o=Recent&flt=true

Results are paged 20 at a time; ``o=Recent`` orders them newest-first. Like
the Workday adapter we stop after MAX_POSTINGS - Microsoft lists thousands of
roles and a poller only needs the recent ones. The search payload carries a
short description inline, so no extra per-job request is needed.

Reachability note: the endpoint sits behind a CDN that selects its
certificate by SNI, so some transparent-proxy / corporate networks fail the
TLS handshake against it. It resolves normally from GitHub Actions, where the
poller runs.
"""

import html
import re
from datetime import datetime

import requests

from scraper.models import Job

API_URL = "https://gcsservices.careers.microsoft.com/search/api/v1/search"
JOB_URL = "https://jobs.careers.microsoft.com/global/en/job/{job_id}"
PAGE_SIZE = 20
MAX_POSTINGS = 200
DESCRIPTION_LIMIT = 500
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config.get("company", "microsoft")
    locale = config.get("locale", "en_us")
    search_text = config.get("search_text", "")

    jobs: list[Job] = []
    for page in range(1, MAX_POSTINGS // PAGE_SIZE + 1):
        response = requests.get(
            API_URL,
            params={
                "q": search_text,
                "l": locale,
                "pg": page,
                "pgSz": PAGE_SIZE,
                "o": "Recent",
                "flt": "true",
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = (response.json().get("operationResult") or {}).get("result") or {}
        postings = result.get("jobs") or []
        if not postings:
            break
        jobs.extend(_to_job(posting, company) for posting in postings)
        total = result.get("totalJobs")
        if total is not None and len(jobs) >= min(total, MAX_POSTINGS):
            break
    return jobs


def _to_job(posting: dict, company: str) -> Job:
    job_id = str(posting.get("jobId", ""))
    props = posting.get("properties") or {}
    return Job(
        id=f"microsoft:{company}:{job_id}",
        title=posting.get("title", ""),
        company=company,
        location=_location(props),
        url=JOB_URL.format(job_id=job_id),
        posted_at=_iso_date(posting.get("postingDate")),
        description=_description(props.get("description") or ""),
        source=f"microsoft/{company}",
    )


def _location(props: dict) -> str:
    # A role can be posted in many cities; keep the primary and note the rest.
    primary = props.get("primaryLocation")
    locations = props.get("locations") or []
    if primary and len(locations) > 1:
        return f"{primary} (+{len(locations) - 1} more)"
    if primary:
        return primary
    return "; ".join(locations)


def _description(text: str) -> str:
    # Some payloads embed HTML; strip tags and unescape entities to plain text.
    plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return " ".join(plain.split())[:DESCRIPTION_LIMIT]


def _iso_date(posting_date: str | None) -> str | None:
    if not posting_date:
        return None
    try:
        return datetime.fromisoformat(posting_date.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return posting_date
