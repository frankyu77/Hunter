"""SAP SuccessFactors career-site (Career Site Builder) adapter.

SuccessFactors career sites (jobs.l3harris.com, careers.qorvo.com,
corningjobs.corning.com) have no public JSON API, but every one publishes a
public RSS feed of its newest postings:

    GET https://{host}/services/rss/job/?locale=en_US&keywords=

Recognise these sites by apply links ending in ``?ats=successfactors``.

Two traps: ``keywords=`` must be present and empty - ``keywords=()`` (what
the site's own feed link uses) answers "Query execution failed" - and the
feed holds only the newest 20 postings with no way to page further, so
MAX_POSTINGS is 20 and closure tracking treats each fetch as a recency
window. Frequent polling keeps up with all but the busiest sites.
"""

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from scraper.models import Job

FEED_URL = "https://{host}/services/rss/job/"
MAX_POSTINGS = 20
DESCRIPTION_LIMIT = 500
TIMEOUT_SECONDS = 30
_JOB_ID = re.compile(r"/(\d+)/?(?:\?|$)")
# Titles read "Engineer II (Camden, AR, US, 71701)": the location rides along.
_TITLE_LOCATION = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def fetch(config: dict) -> list[Job]:
    company = config["company"]
    host = config["host"]
    response = requests.get(
        FEED_URL.format(host=host),
        params={"locale": config.get("locale", "en_US"), "keywords": ""},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    if root.tag != "rss":
        raise ValueError(f"{host}: feed error: {response.text[:200]}")

    jobs = []
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        match = _JOB_ID.search(link)
        if not match:
            continue
        title, location = _split_title(item.findtext("title") or "")
        jobs.append(
            Job(
                id=f"successfactors:{host}:{match.group(1)}",
                title=title,
                company=company,
                location=location,
                url=link.split("?", 1)[0],
                posted_at=_iso(item.findtext("pubDate")),
                description=_description(item.findtext("description") or ""),
                source=f"successfactors/{company}",
            )
        )
    return jobs


def _split_title(raw: str) -> tuple[str, str]:
    raw = " ".join(raw.split())
    match = _TITLE_LOCATION.match(raw)
    if not match:
        return raw, ""
    # Drop the trailing postal code: "Camden, AR, US, 71701" -> "Camden, AR, US".
    parts = [part.strip() for part in match.group(2).split(",")]
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return match.group(1), ", ".join(parts)


def _iso(pub_date: str | None) -> str | None:
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _description(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", markup)
    return " ".join(html.unescape(text).split())[:DESCRIPTION_LIMIT]
