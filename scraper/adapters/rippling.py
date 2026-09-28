"""Rippling ATS adapter.

Public, unauthenticated board API, returning every open job in one list:

    GET https://api.rippling.com/platform/api/ats/v1/board/{company}/jobs

``company`` is the board slug: for
https://ats.rippling.com/flexai/jobs/93ada67c-... it is "flexai". Each job
carries its own board URL.

The newer https://ats.rippling.com/api/v2/board/{company}/jobs endpoint
works too but pages its results; v1 returns the whole (small) board at
once. Neither carries posting dates or descriptions.
"""

import requests

from scraper.models import Job

API_URL = "https://api.rippling.com/platform/api/ats/v1/board/{company}/jobs"
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    response = requests.get(API_URL.format(company=company), timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return [
        Job(
            id=f"rippling:{company}:{posting['uuid']}",
            title=posting.get("name", ""),
            company=company,
            location=(posting.get("workLocation") or {}).get("label", ""),
            url=posting.get("url", ""),
            posted_at=None,
            description="",
            source=f"rippling/{company}",
        )
        for posting in response.json()
    ]
