"""Eightfold career sites on the older v2 API (Netflix).

Most Eightfold boards answer the PCSX search (see ``eightfold``) and refuse
the older endpoint. A few are the reverse: Netflix's careers site
(explore.jobs.netflix.net) answers PCSX with 403 "PCSX is not enabled for
this user" but still serves

    GET https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=10

Pages are fixed at 10 (``num`` is ignored), and ``sort_by=timestamp`` does
not actually sort: a later page can hold newer postings than the first. So,
unlike PCSX, this cannot stop at a newest-first slice - it reads the whole
board (Netflix: ~460 postings, ~47 requests, ~10s). MAX_POSTINGS is only a
safety stop, far above any real board, and NEWEST_FIRST = False tells
closure tracking not to trust a capped fetch as a recency window.
"""

from datetime import UTC, datetime

import requests

from scraper.models import Job

API_URL = "https://{host}/api/apply/v2/jobs"
JOB_URL = "https://{host}/careers/job/{id}"
PAGE_SIZE = 10
MAX_POSTINGS = 3000
NEWEST_FIRST = False
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    tenant = config.get("tenant", company)
    host = config["host"]
    domain = config["domain"]
    jobs: list[Job] = []
    for start in range(0, MAX_POSTINGS, PAGE_SIZE):
        response = requests.get(
            API_URL.format(host=host),
            params={"domain": domain, "start": start, "num": PAGE_SIZE},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        positions = payload.get("positions") or []
        jobs.extend(_to_job(position, company, tenant, host) for position in positions)
        if len(positions) < PAGE_SIZE or len(jobs) >= payload.get("count", 0):
            break
    return jobs


def _to_job(position: dict, company: str, tenant: str, host: str) -> Job:
    created = position.get("t_create")
    return Job(
        id=f"eightfold:{tenant}:{position['id']}",
        title=" ".join((position.get("name") or "").split()),
        company=company,
        location="; ".join(position.get("locations") or [position.get("location") or ""]),
        url=position.get("canonicalPositionUrl") or JOB_URL.format(host=host, id=position["id"]),
        posted_at=(datetime.fromtimestamp(created, tz=UTC).isoformat(timespec="seconds")
                   if created else None),
        description="",
        source=f"eightfold_v2/{company}",
    )
