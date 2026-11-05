"""Workday adapter.

Every Workday career site exposes the same unauthenticated JSON endpoint
its own frontend uses:

    POST https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

The three config values come straight from the board URL. For
https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site the
tenant is "ngc", the host is "wd1", and the site is
"Northrop_Grumman_External_Site".

Results come 20 per page. Large tenants list thousands of postings, so
fetching everything would cost hundreds of requests per company per run;
a poller only needs the recent ones, so we normally stop after
MAX_POSTINGS.

The trap: the order is only *mostly* newest-first. NVIDIA and Boeing list
today's postings, then older ones, as you'd hope - but TD interleaves, with
a second block of "Posted Today" jobs at positions ~100-230 of 1.7k, so a
flat 200 cap silently missed ~30 new jobs a day. So past MAX_POSTINGS we
keep paging while a page still holds a today/yesterday posting, up to
HARD_MAX_POSTINGS. An ordered board stops at 200 as before. Whether a
given fetch came back ordered is checked at runtime (``main.fetch_all``),
since closure tracking may only trust a capped slice that did.

The list endpoint carries no description (that would be one extra request
per job) - filters match on title only, so descriptions stay empty.

Throttling: some tenants answer a share of requests with 429 at random,
however they are spaced (Salesforce, Oct 2026: about half, from any IP).
Restarting the whole board on a 429 - what the generic retry in
``main.fetch_with_retry`` does - throws away the pages already fetched and
rarely gets ten pages through in one go. So a refused page is retried on its
own, PAGE_RETRIES times, PAGE_RETRY_WAIT_SECONDS apart. If it still fails,
the error is marked ``already_retried`` so main doesn't retry the whole
board on top: a board that is refused outright then costs seconds, not
minutes, which matters when many tenants are throttled at once.

``max_postings`` in a source's config lowers the cap for one board (e.g. a
heavily throttled one), and is then a hard stop: the "keep paging past the
cap while postings are recent" rule below doesn't apply to it.
``main.fetch_all`` reads the same key, so closure tracking still treats the
shorter fetch as the newest slice rather than the whole board.

A few tenants live on Workday's second domain instead, with the tenant in
the path rather than the subdomain: for
https://wd5.myworkdaysite.com/recruiting/microchiphr/External set
``domain: myworkdaysite.com`` (tenant "microchiphr", host "wd5", site
"External"). The API is identical apart from the URL.
"""

import re
import time
from datetime import UTC, datetime, timedelta

import requests

from scraper.models import Job

API_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
BOARD_URL = "https://{tenant}.{host}.myworkdayjobs.com/{site}"
SITE_DOMAIN = "myworkdaysite.com"
SITE_API_URL = "https://{host}.myworkdaysite.com/wday/cxs/{tenant}/{site}/jobs"
SITE_BOARD_URL = "https://{host}.myworkdaysite.com/recruiting/{tenant}/{site}"
PAGE_SIZE = 20
MAX_POSTINGS = 200
HARD_MAX_POSTINGS = 1000
TIMEOUT_SECONDS = 30
PAGE_RETRIES = 4
PAGE_RETRY_WAIT_SECONDS = 2


def fetch(config: dict) -> list[Job]:
    tenant = config["tenant"]
    host = config["host"]
    site = config["site"]
    company = config.get("company") or tenant
    on_site_domain = config.get("domain") == SITE_DOMAIN
    api_url = (SITE_API_URL if on_site_domain else API_URL).format(
        tenant=tenant, host=host, site=site
    )
    base_url = (SITE_BOARD_URL if on_site_domain else BOARD_URL).format(
        tenant=tenant, host=host, site=site
    )

    soft_cap = config.get("max_postings", MAX_POSTINGS)
    hard_cap = soft_cap if "max_postings" in config else HARD_MAX_POSTINGS
    jobs: list[Job] = []
    total = hard_cap
    recent_on_last_page = False
    for offset in range(0, hard_cap, PAGE_SIZE):
        if offset >= total or (offset >= soft_cap and not recent_on_last_page):
            break
        payload = _page(api_url, {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": config.get("search_text", ""),
        })
        if offset == 0:
            # Some tenants report total=0 on every page after the first,
            # so only the first page's count can be trusted.
            total = min(payload.get("total", 0), hard_cap)
        postings = payload.get("jobPostings", [])
        if not postings:
            break
        jobs.extend(_to_job(posting, tenant, company, base_url) for posting in postings)
        recent_on_last_page = any(_is_recent(p.get("postedOn", "")) for p in postings)
    return jobs


def _page(api_url: str, body: dict) -> dict:
    """One page, retrying a 429 on just this page (see module docstring)."""
    for attempt in range(PAGE_RETRIES + 1):
        response = requests.post(api_url, json=body, timeout=TIMEOUT_SECONDS)
        if response.status_code != 429 or attempt == PAGE_RETRIES:
            break
        time.sleep(PAGE_RETRY_WAIT_SECONDS)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        exc.already_retried = response.status_code == 429
        raise
    return response.json()


def _is_recent(posted_on: str) -> bool:
    text = posted_on.lower()
    return "today" in text or "yesterday" in text


def _to_job(posting: dict, tenant: str, company: str, base_url: str) -> Job:
    path = posting.get("externalPath", "")
    bullets = posting.get("bulletFields") or []
    req_id = bullets[0] if bullets else path
    return Job(
        id=f"workday:{tenant}:{req_id}",
        title=posting.get("title", ""),
        company=company,
        location=posting.get("locationsText", ""),
        url=f"{base_url}{path}",
        posted_at=_parse_posted_on(posting.get("postedOn", "")),
        description="",
        source=f"workday/{company}",
    )


def _parse_posted_on(posted_on: str) -> str | None:
    """Turn Workday's fuzzy "Posted Today" / "Posted 3 Days Ago" into an ISO
    date so the max_age_days filter can reason about it. "30+ Days Ago" and
    anything unrecognized become None (undated jobs pass filters: never-miss)."""
    text = posted_on.lower()
    if "today" in text:
        days = 0
    elif "yesterday" in text:
        days = 1
    else:
        match = re.search(r"(\d+)\+?\s+days?\s+ago", text)
        if not match or "+" in text:
            return None
        days = int(match.group(1))
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
