"""Workable adapter.

Public, unauthenticated endpoint the hosted careers page itself calls:

    POST https://apply.workable.com/api/v3/accounts/{company}/jobs
    body: {"query": "", "location": [], "department": [], "worktype": [], "remote": []}

``company`` is the account slug from the board URL: for
https://apply.workable.com/tickpick/j/5840ECEB50/ it is "tickpick". The
response pages with an opaque ``nextPage`` token, sent back as ``token``.
The job page is https://apply.workable.com/{company}/j/{shortcode}/.

Don't use the older /api/v1/widget endpoint: it omits posting dates, which
the max_age_days filter needs. No description in the list payload.
"""

import requests

from scraper.models import Job

API_URL = "https://apply.workable.com/api/v3/accounts/{company}/jobs"
JOB_URL = "https://apply.workable.com/{company}/j/{shortcode}/"
MAX_PAGES = 20  # boards are small; a hard stop in case a token ever loops
TIMEOUT_SECONDS = 30
_EMPTY_QUERY = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    jobs: list[Job] = []
    body: dict = dict(_EMPTY_QUERY)
    for _ in range(MAX_PAGES):
        response = requests.post(
            API_URL.format(company=company), json=body, timeout=TIMEOUT_SECONDS
        )
        response.raise_for_status()
        payload = response.json()
        jobs.extend(_to_job(posting, company) for posting in payload.get("results", []))
        if not payload.get("nextPage"):
            break
        body = dict(_EMPTY_QUERY) | {"token": payload["nextPage"]}
    return jobs


def _to_job(posting: dict, company: str) -> Job:
    return Job(
        id=f"workable:{company}:{posting['shortcode']}",
        title=posting.get("title", ""),
        company=company,
        location=_location(posting),
        url=JOB_URL.format(company=company, shortcode=posting["shortcode"]),
        posted_at=posting.get("published"),
        description="",
        source=f"workable/{company}",
    )


def _location(posting: dict) -> str:
    places = []
    for place in posting.get("locations") or [posting.get("location") or {}]:
        if place.get("hidden"):
            continue
        parts = [place.get("city"), place.get("region"), place.get("country")]
        if text := ", ".join(part for part in parts if part):
            places.append(text)
    location = "; ".join(places)
    if posting.get("remote"):
        location = f"{location} (Remote)" if location else "Remote"
    return location
