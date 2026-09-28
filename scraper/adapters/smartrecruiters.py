"""SmartRecruiters adapter.

Public, unauthenticated postings API:

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings
        ?limit=100&offset=0[&country=us][&q=engineer]

``company`` is the identifier in the board URL: for
https://jobs.smartrecruiters.com/BoschGroup/744000146524429 it is
"BoschGroup". The job page is https://jobs.smartrecruiters.com/{company}/{id}.

The tempting shortcut is to read only the first page, as the capped Workday
adapter does - but this list is NOT reliably newest-first: ``releasedDate``
stays at a posting's first release, so a live 2026 req can sort behind
2017 ones (Dell's does). Every page up to MAX_POSTINGS is read instead, and
``NEWEST_FIRST = False`` tells closure tracking a capped fetch here is not a
recency window. Big boards run to tens of thousands of postings (CROSSMARK:
21k), so ``countries`` (one query per country code) and ``query`` (full-text)
narrow them server-side - Bosch drops from 4.8k to 192 with ``us`` +
"engineer". The list carries no description (one extra request per job).
"""

import logging

import requests

from scraper.models import Job

log = logging.getLogger(__name__)

API_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
JOB_URL = "https://jobs.smartrecruiters.com/{company}/{id}"
PAGE_SIZE = 100
MAX_POSTINGS = 1000
NEWEST_FIRST = False
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    base = {"q": config["query"]} if config.get("query") else {}
    countries = config.get("countries") or [None]

    seen: set[str] = set()
    jobs: list[Job] = []
    for country in countries:
        params = base | ({"country": country} if country else {})
        for offset in range(0, MAX_POSTINGS, PAGE_SIZE):
            response = requests.get(
                API_URL.format(company=company),
                params=params | {"limit": PAGE_SIZE, "offset": offset},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            postings = payload.get("content", [])
            for posting in postings:
                if posting["id"] not in seen:  # a multi-country req appears per country
                    seen.add(posting["id"])
                    jobs.append(_to_job(posting, company))
            if len(postings) < PAGE_SIZE or offset + PAGE_SIZE >= payload.get("totalFound", 0):
                break
        else:
            log.warning(
                "smartrecruiters/%s: %d postings exceed the %d cap and arrive unordered, so "
                "some are never seen - narrow with countries or query.", company,
                payload.get("totalFound", 0), MAX_POSTINGS,
            )
    return jobs[:MAX_POSTINGS]


def _to_job(posting: dict, company: str) -> Job:
    location = posting.get("location") or {}
    place = location.get("fullLocation") or ", ".join(
        part for part in (location.get("city"), location.get("region"), location.get("country"))
        if part
    )
    if location.get("remote") and "remote" not in place.lower():
        place = f"{place} (Remote)" if place else "Remote"
    return Job(
        id=f"smartrecruiters:{company}:{posting['id']}",
        title=posting.get("name", ""),
        company=(posting.get("company") or {}).get("name") or company,
        location=place,
        url=JOB_URL.format(company=company, id=posting["id"]),
        posted_at=posting.get("releasedDate"),
        description="",
        source=f"smartrecruiters/{company}",
    )
