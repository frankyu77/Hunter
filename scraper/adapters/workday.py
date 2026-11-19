"""Workday adapter.

Every Workday career site exposes the same unauthenticated JSON endpoint
its own frontend uses:

    POST https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

The three config values come straight from the board URL. For
https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site the
tenant is "ngc", the host is "wd1", and the site is
"Northrop_Grumman_External_Site".

A few tenants live on Workday's second domain instead, with the tenant in
the path rather than the subdomain: for
https://wd5.myworkdaysite.com/recruiting/microchiphr/External set
``domain: myworkdaysite.com`` (tenant "microchiphr", host "wd5", site
"External"). The API is identical apart from the URL.

The list endpoint carries no description (that would be one extra request
per job) - filters match on title only, so descriptions stay empty.

Throttling is the whole design problem here. Since mid-November 2026
Workday's edge answers GitHub Actions runners with an HTML 429 page on most
requests - every tenant, every wdN host. Runner IPs are shared, so we can't
control all of that, but we can stop being a large part of it. The old
shape (all ~25 tenants every 5-minute run, 10-50 pages each, whole board
thrown away on one refused page) made several hundred requests an hour and
lost the big boards almost every run. So, from coarse to fine:

- Pacing (``PACING``, applied by ``scraper.pacing``): a board is polled at
  most every ``every_minutes``, the longest-waiting boards first, at most
  ``per_run`` of them and for at most ``budget_seconds`` per run, with a
  short random pause between boards. That spreads the tenants across runs
  instead of hitting all of them at once, while each one still comes round
  about every half hour. If ``breaker_after`` boards in a row come back
  throttled, the run stops touching Workday; the untouched boards stay
  first in line for the next run.
- Depth: normally MAX_POSTINGS (3 pages). The order is only *mostly*
  newest-first - TD interleaves a second block of "Posted Today" jobs a
  hundred or so positions in - so past the cap we keep paging while a page
  still holds a today/yesterday posting, up to HARD_MAX_POSTINGS. Whether a
  given fetch came back ordered is checked at runtime
  (``main.fetch_all``), since closure tracking may only trust a capped
  slice that did. ``max_postings`` in a source's config lowers the cap for
  one board and is then a hard stop; ``main.fetch_all`` reads the same key.
- Pages: a random 1-3 s pause between pages; a refused page is retried on
  its own (restarting the board would throw away the pages already read)
  with jittered backoff (RETRY_WAITS), or after the server's Retry-After if
  that is longer. A Retry-After beyond MAX_WAIT_SECONDS, or a wait past the
  run's Workday deadline, gives up at once.
- Partial results: if a later page is still refused, the pages already
  read are returned as a ``PartialFetch``, which main treats as the newest
  slice of the board (never the whole board, so nothing is marked closed
  for being on the pages we didn't get). Only a refused *first* page fails
  the board, marked ``already_retried`` so main doesn't retry it again.
"""

import random
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

from scraper.models import Job, PartialFetch

API_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
BOARD_URL = "https://{tenant}.{host}.myworkdayjobs.com/{site}"
SITE_DOMAIN = "myworkdaysite.com"
SITE_API_URL = "https://{host}.myworkdaysite.com/wday/cxs/{tenant}/{site}/jobs"
SITE_BOARD_URL = "https://{host}.myworkdaysite.com/recruiting/{tenant}/{site}"
PAGE_SIZE = 20
MAX_POSTINGS = 60
HARD_MAX_POSTINGS = 200
TIMEOUT_SECONDS = 30
RETRY_WAITS = (2, 5, 15, 30)  # before each retry of a refused page, +/-20% jitter
MAX_WAIT_SECONDS = 60  # a longer Retry-After means "not this run"
PAGE_PAUSE_SECONDS = (1, 3)

# Read by scraper.pacing (see the module docstring). About 25 tenants at up
# to 9 a run, one run every 5-10 minutes, brings each round every ~20-30
# minutes when Workday isn't refusing us. The rest of a run's fetches take
# ~165 s, so 75 s here keeps the whole job near 4 minutes, inside the
# 5-minute schedule. (Unthrottled, a 3-page board costs ~7 s.)
PACING = {
    "every_minutes": 20,
    "per_run": 9,
    "budget_seconds": 75,
    "pause_seconds": (1, 3),
    "breaker_after": 2,
}


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
    # time.monotonic() by which this run's Workday time is up; set by
    # scraper.pacing, absent when the adapter is called on its own.
    deadline = config.get("_deadline")

    soft_cap = config.get("max_postings", MAX_POSTINGS)
    hard_cap = soft_cap if "max_postings" in config else HARD_MAX_POSTINGS
    jobs: list[Job] = []
    total = hard_cap
    recent_on_last_page = False
    for offset in range(0, hard_cap, PAGE_SIZE):
        if offset >= total or (offset >= soft_cap and not recent_on_last_page):
            break
        if offset:
            if _past(deadline):
                return PartialFetch(jobs)
            time.sleep(random.uniform(*PAGE_PAUSE_SECONDS))
        try:
            payload = _page(api_url, {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": config.get("search_text", ""),
            }, deadline)
        except requests.exceptions.HTTPError as exc:
            if jobs and getattr(exc, "already_retried", False):
                return PartialFetch(jobs, throttled=True)
            raise
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


def _page(api_url: str, body: dict, deadline: float | None = None) -> dict:
    """One page, retrying a 429 on just this page (see module docstring)."""
    for attempt in range(len(RETRY_WAITS) + 1):
        response = requests.post(api_url, json=body, timeout=TIMEOUT_SECONDS)
        if response.status_code != 429 or attempt == len(RETRY_WAITS):
            break
        wait = _wait(RETRY_WAITS[attempt], response)
        if wait is None or (deadline is not None and time.monotonic() + wait > deadline):
            break
        time.sleep(wait)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        exc.already_retried = response.status_code == 429
        raise
    return response.json()


def _wait(backoff: float, response: requests.Response) -> float | None:
    """Seconds to wait before retrying a refused page, or None to give up:
    the jittered backoff, or the server's Retry-After if that is longer."""
    wait = backoff * random.uniform(0.8, 1.2)
    retry_after = _retry_after(response.headers.get("Retry-After"))
    if retry_after is not None:
        if retry_after > MAX_WAIT_SECONDS:
            return None
        wait = max(wait, retry_after)
    return wait


def _retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


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
