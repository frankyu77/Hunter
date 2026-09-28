"""Eightfold AI career-site adapter.

Eightfold-hosted career sites (qualcomm.eightfold.ai) read their jobs from a
public search endpoint on the same host:

    GET https://{tenant}.eightfold.ai/api/pcsx/search
        ?domain={domain}&query=&location=&start=0&sort_by=timestamp

``domain`` is the company's own email domain ("qualcomm.com"). The job page
is https://{tenant}.eightfold.ai/careers/job/{id}.

The tempting wrong door is /api/apply/v2/jobs, which older guides describe:
it now answers 403 "Not authorized for PCSX". Results come 10 per page
(``num`` is ignored), newest-first, so like Workday we stop after
MAX_POSTINGS. No descriptions in the search payload.
"""

from datetime import UTC, datetime

import requests

from scraper.models import Job

API_URL = "https://{tenant}.eightfold.ai/api/pcsx/search"
JOB_URL = "https://{tenant}.eightfold.ai/careers/job/{id}"
PAGE_SIZE = 10
MAX_POSTINGS = 100
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    tenant = config["tenant"]
    domain = config["domain"]
    jobs: list[Job] = []
    for start in range(0, MAX_POSTINGS, PAGE_SIZE):
        response = requests.get(
            API_URL.format(tenant=tenant),
            params={"domain": domain, "query": "", "location": "", "start": start,
                    "sort_by": "timestamp"},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        positions = data.get("positions") or []
        jobs.extend(_to_job(position, company, tenant) for position in positions)
        if len(positions) < PAGE_SIZE or len(jobs) >= data.get("count", 0):
            break
    return jobs


def _to_job(position: dict, company: str, tenant: str) -> Job:
    posted = position.get("postedTs")
    return Job(
        id=f"eightfold:{tenant}:{position['id']}",
        title=position.get("name", ""),
        company=company,
        location="; ".join(position.get("locations") or []),
        url=JOB_URL.format(tenant=tenant, id=position["id"]),
        posted_at=(
            datetime.fromtimestamp(posted, tz=UTC).isoformat(timespec="seconds")
            if posted else None
        ),
        description="",
        source=f"eightfold/{company}",
    )
