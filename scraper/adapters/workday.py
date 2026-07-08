"""Workday adapter.

Every Workday career site exposes the same unauthenticated JSON endpoint
its own frontend uses:

    POST https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

The three config values come straight from the board URL. For
https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site the
tenant is "ngc", the host is "wd1", and the site is
"Northrop_Grumman_External_Site".

Results are newest-first and capped at 20 per page. Large tenants list
thousands of postings, so fetching everything would cost hundreds of
requests per company per run; a poller only needs the recent postings,
so we stop after MAX_POSTINGS. The list endpoint carries no description
(that would be one extra request per job) - filters match on title only,
so descriptions stay empty.
"""

import re
from datetime import UTC, datetime, timedelta

import requests

from scraper.models import Job

API_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
BOARD_URL = "https://{tenant}.{host}.myworkdayjobs.com/{site}"
PAGE_SIZE = 20
MAX_POSTINGS = 200
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    tenant = config["tenant"]
    host = config["host"]
    site = config["site"]
    company = config.get("company") or tenant
    base_url = BOARD_URL.format(tenant=tenant, host=host, site=site)

    jobs: list[Job] = []
    total = MAX_POSTINGS
    for offset in range(0, MAX_POSTINGS, PAGE_SIZE):
        if offset >= total:
            break
        response = requests.post(
            API_URL.format(tenant=tenant, host=host, site=site),
            json={
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": config.get("search_text", ""),
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if offset == 0:
            # Some tenants report total=0 on every page after the first,
            # so only the first page's count can be trusted.
            total = min(payload.get("total", 0), MAX_POSTINGS)
        postings = payload.get("jobPostings", [])
        if not postings:
            break
        jobs.extend(_to_job(posting, tenant, company, base_url) for posting in postings)
    return jobs


def _to_job(posting: dict, tenant: str, company: str, base_url: str) -> Job:
    path = posting.get("externalPath", "")
    bullets = posting.get("bulletFields") or []
    req_id = bullets[0] if bullets else path
    return Job(
        id=f"workday:{tenant}:{req_id}",
        title=posting.get("title", ""),
        company=company,
        location=posting.get("locationsText", ""),
        url=f"{base_url}{path}",
        posted_at=_parse_posted_on(posting.get("postedOn", "")),
        description="",
        source=f"workday/{company}",
    )


def _parse_posted_on(posted_on: str) -> str | None:
    """Turn Workday's fuzzy "Posted Today" / "Posted 3 Days Ago" into an ISO
    date so the max_age_days filter can reason about it. "30+ Days Ago" and
    anything unrecognized become None (undated jobs pass filters: never-miss)."""
    text = posted_on.lower()
    if "today" in text:
        days = 0
    elif "yesterday" in text:
        days = 1
    else:
        match = re.search(r"(\d+)\+?\s+days?\s+ago", text)
        if not match or "+" in text:
            return None
        days = int(match.group(1))
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
