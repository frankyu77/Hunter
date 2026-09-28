"""BambooHR adapter.

Every BambooHR careers site serves its job list as JSON from the same
path its page loads, unauthenticated:

    GET https://{company}.bamboohr.com/careers/list   (Accept: application/json)

``company`` is the subdomain: for https://lexical.bamboohr.com/careers/73
it is "lexical". The job page is https://{company}.bamboohr.com/careers/{id}.

The list has no posting dates or descriptions (undated jobs pass the age
filter - never-miss). ``atsLocation`` is usually empty; ``location`` is the
field actually filled in.
"""

import requests

from scraper.models import Job

API_URL = "https://{company}.bamboohr.com/careers/list"
JOB_URL = "https://{company}.bamboohr.com/careers/{id}"
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    response = requests.get(
        API_URL.format(company=company),
        headers={"Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return [
        Job(
            id=f"bamboohr:{company}:{posting['id']}",
            title=posting.get("jobOpeningName", ""),
            company=company,
            location=_location(posting),
            url=JOB_URL.format(company=company, id=posting["id"]),
            posted_at=None,
            description="",
            source=f"bamboohr/{company}",
        )
        for posting in response.json().get("result", [])
    ]


def _location(posting: dict) -> str:
    for place in (posting.get("location") or {}, posting.get("atsLocation") or {}):
        parts = [place.get("city"), place.get("state") or place.get("province"),
                 place.get("country")]
        if text := ", ".join(part for part in parts if part):
            return f"{text} (Remote)" if posting.get("isRemote") else text
    return "Remote" if posting.get("isRemote") else ""
