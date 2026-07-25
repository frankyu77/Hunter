"""Oracle Recruiting Cloud (ORC) adapter.

Companies on Oracle Fusion HCM expose their "Candidate Experience" job list
through a public, unauthenticated REST endpoint - the same one the careers
site's own frontend calls:

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &finder=findReqs;siteNumber={site_number},limit=25,offset=0,sortBy=POSTING_DATES_DESC

Config comes straight from the careers URL. For Dell's
https://iawmqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/careers
the host is "iawmqy.fa.ocs.oraclecloud.com", the apply-link site name is
"careers", and the API siteNumber is "CX_1" (the common default). Some
tenants front Oracle on a custom domain, e.g. host "careers.ti.com". The
site name (apply URL) and siteNumber (API) are independent, so both are
configurable.

Results are newest-first; like the Workday adapter we stop after MAX_POSTINGS
since a poller only needs the recent ones. Full descriptions aren't in the
list payload, so they stay best-effort (the short blurb when present).
"""

import html
import re

import requests

from scraper.models import Job

API_URL = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
JOB_URL = "https://{host}/hcmUI/CandidateExperience/en/sites/{site_name}/job/{job_id}"
PAGE_SIZE = 25
MAX_POSTINGS = 200
DESCRIPTION_LIMIT = 500
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    host = config["host"]
    site_number = config.get("site_number", "CX_1")
    site_name = config.get("site_name", "CX")

    url = API_URL.format(host=host)
    jobs: list[Job] = []
    for offset in range(0, MAX_POSTINGS, PAGE_SIZE):
        response = requests.get(
            url,
            params={
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": (
                    f"findReqs;siteNumber={site_number},limit={PAGE_SIZE},"
                    f"offset={offset},sortBy=POSTING_DATES_DESC"
                ),
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        if not items:
            break
        result = items[0]
        postings = result.get("requisitionList") or []
        if not postings:
            break
        jobs.extend(_to_job(posting, company, host, site_name) for posting in postings)
        total = result.get("TotalJobsCount")
        if total is not None and len(jobs) >= min(total, MAX_POSTINGS):
            break
    return jobs


def _to_job(posting: dict, company: str, host: str, site_name: str) -> Job:
    job_id = str(posting.get("Id", ""))
    return Job(
        id=f"oracle:{company}:{job_id}",
        title=posting.get("Title", ""),
        company=company,
        location=_location(posting),
        url=JOB_URL.format(host=host, site_name=site_name, job_id=job_id),
        posted_at=posting.get("PostedDate") or None,  # already an ISO date
        description=_description(posting.get("ShortDescriptionStr") or ""),
        source=f"oracle/{company}",
    )


def _location(posting: dict) -> str:
    primary = posting.get("PrimaryLocation") or ""
    extra = posting.get("secondaryLocations") or []
    if primary and extra:
        return f"{primary} (+{len(extra)} more)"
    return primary


def _description(text: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return " ".join(plain.split())[:DESCRIPTION_LIMIT]
