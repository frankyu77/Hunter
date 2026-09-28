"""TikTok careers adapter (lifeattiktok.com).

TikTok runs its own careers site. Its frontend reads a public, but
unofficial, search endpoint:

    POST https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts
    body: {"keyword": "", "limit": 100, "offset": 0, "recruitment_id_list": [],
           "job_category_id_list": [], "subject_id_list": [], "location_code_list": []}

It answers "invalid request" unless the portal headers below are sent, and
being unofficial it can change without notice - health tracking will flag
it if it starts failing. The job page is https://lifeattiktok.com/search/{id}.

Two traps. Results are NOT newest-first (``NEWEST_FIRST = False``), so every
page up to MAX_POSTINGS is read - and the ~4.3k-job board must be narrowed
below that cap or postings past it are never seen. ``recruitment_ids`` does
it server-side: "2" is early careers (new grad "201" + intern "202", ~1.7k).
``keyword`` is fuzzy ("intern" matches 3k jobs) and the location filter only
takes city codes, so neither narrows reliably. A board still over the cap is
logged as a warning. And there is no date field - but ids are snowflakes
whose top 32 bits are the creation time in Unix seconds, which is used as
the posting date. ByteDance's own site (jobs.bytedance.com) looks similar
but rejects this call ("site not exist").
"""

import logging
from datetime import UTC, datetime

import requests

from scraper.models import Job

log = logging.getLogger(__name__)

API_URL = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"
JOB_URL = "https://lifeattiktok.com/search/{id}"
PAGE_SIZE = 100
MAX_POSTINGS = 2000
NEWEST_FIRST = False
DESCRIPTION_LIMIT = 500
TIMEOUT_SECONDS = 30
HEADERS = {
    "website-path": "tiktok",
    "portal-channel": "tiktok",
    "portal-platform": "pc",
    "env": "prod",
    "accept-language": "en-US",
    "Referer": "https://lifeattiktok.com/",
}


def fetch(config: dict) -> list[Job]:
    company = config.get("company", "tiktok")
    jobs: list[Job] = []
    for offset in range(0, MAX_POSTINGS, PAGE_SIZE):
        response = requests.post(
            API_URL,
            headers=HEADERS,
            json={
                "keyword": config.get("keyword", ""),
                "limit": PAGE_SIZE,
                "offset": offset,
                "recruitment_id_list": [str(i) for i in config.get("recruitment_ids") or []],
                "job_category_id_list": [],
                "subject_id_list": [],
                "location_code_list": [],
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise ValueError(f"TikTok search error: {payload}")
        data = payload.get("data") or {}
        postings = data.get("job_post_list") or []
        jobs.extend(_to_job(posting, company) for posting in postings)
        if len(postings) < PAGE_SIZE or len(jobs) >= data.get("count", 0):
            break
    else:
        log.warning(
            "tiktok/%s: %d postings exceed the %d cap and arrive unordered, so some are "
            "never seen - narrow with recruitment_ids.", company, data.get("count", 0),
            MAX_POSTINGS,
        )
    return jobs


def _to_job(posting: dict, company: str) -> Job:
    return Job(
        id=f"tiktok:{company}:{posting['id']}",
        title=posting.get("title", ""),
        company=company,
        location=_location(posting.get("city_info")),
        url=JOB_URL.format(id=posting["id"]),
        posted_at=_snowflake_time(posting["id"]),
        description=" ".join((posting.get("description") or "").split())[:DESCRIPTION_LIMIT],
        source=f"tiktok/{company}",
    )


def _location(city: dict | None) -> str:
    # city -> state -> country, each a parent of the last.
    names = []
    while city:
        if name := city.get("en_name") or city.get("i18n_name") or city.get("name"):
            names.append(name)
        city = city.get("parent")
    return ", ".join(names)


def _snowflake_time(post_id: str) -> str | None:
    try:
        seconds = int(post_id) >> 32
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
