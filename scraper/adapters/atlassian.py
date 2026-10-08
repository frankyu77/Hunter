"""Atlassian careers adapter (atlassian.com/company/careers).

Atlassian's careers page reads every open posting from one public JSON
endpoint on its own site:

    GET https://www.atlassian.com/endpoint/careers/listings

That returns the whole board (a few hundred postings) in one response, so
there is no paging and no cap. The tempting wrong door is the iCIMS portal
its apply links point at (globalcareers-atlassian.icims.com): a native iCIMS
portal has no public API, which is why discovery used to file Atlassian
under "iCIMS portals" with no adapter.

There is no posted date, only the portal's ``updatedDate``
("2026-09-24 03:33 PM"); it stands in as ``posted_at``, which is close
enough for the max-age filter. ``locations`` is a list of strings like
"Seattle - United States -   Seattle, Washington  United States"; they are
whitespace-normalised and joined, which keeps the country names the region
classifier needs. The job page is the portal URL.
"""

import html
import re
from datetime import datetime

import requests

from scraper.models import Job

API_URL = "https://www.atlassian.com/endpoint/careers/listings"
DESCRIPTION_LIMIT = 500
MAX_LOCATIONS = 3
TIMEOUT_SECONDS = 30


def fetch(config: dict) -> list[Job]:
    company = config.get("company", "atlassian")
    response = requests.get(API_URL, timeout=TIMEOUT_SECONDS,
                            headers={"Accept": "application/json"})
    response.raise_for_status()
    return [_to_job(posting, company) for posting in response.json() or []]


def _to_job(posting: dict, company: str) -> Job:
    portal = posting.get("portalJobPost") or {}
    locations = [" ".join(place.split()) for place in posting.get("locations") or []]
    return Job(
        id=f"atlassian:{company}:{posting['id']}",
        title=" ".join((posting.get("title") or "").split()),
        company=company,
        location="; ".join(locations[:MAX_LOCATIONS]),
        url=portal.get("portalUrl") or posting.get("applyUrl") or "",
        posted_at=_iso(portal.get("updatedDate")),
        description=_text(posting.get("overview") or "")[:DESCRIPTION_LIMIT],
        source=f"atlassian/{company}",
    )


def _iso(updated: str | None) -> str | None:
    # "2026-09-24 03:33 PM"
    if not updated:
        return None
    try:
        return datetime.strptime(updated, "%Y-%m-%d %I:%M %p").date().isoformat()
    except ValueError:
        return None


def _text(markup: str) -> str:
    # A tag cut off at the end (no closing ">") is stripped too.
    return " ".join(html.unescape(re.sub(r"<[^>]*(?:>|$)", " ", markup)).split())
