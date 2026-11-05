"""Find company boards through web search, then poll them directly.

scraper.discovery only learns about a company once one of its jobs shows up
in an aggregator feed. Plenty never do - startups on Ashby or Greenhouse that
SimplifyJobs doesn't list. A site-restricted web search finds them: the same
idea as typing ``site:jobs.ashbyhq.com intern software Canada`` into Google.

Search is used to find *boards*, never as the job feed. A search index lags
hours to days behind a board, while polling the board's own API sees a new
posting within one run. So once a day this runs a handful of searches,
turns each result URL into a board (``discovery.board_for``, the same parser
the feeds use), and checks the board live with its real adapter: it is
added only if it lists at least one open job passing your sources.yaml
filters right now. Added boards become ordinary auto-added sources
(scraper.autosources, how="search"): polled every run, shown on the
dashboard's Sources page with Remove, announced in Telegram.

The search API is Tavily (https://api.tavily.com/search): Google's Custom
Search JSON API closed to new customers in January 2026, and Brave's no
longer has a card-free tier. Tavily's free plan is 1,000 basic searches a
month with no card; ``include_domains`` restricts results to job-board
sites, like ``site:``. With the default ~7 queries a day that is ~210 a
month. Off entirely (no requests) without the TAVILY_API_KEY secret.

Guardrails: one round a day; a board checked and not added isn't re-checked
for RECHECK_DAYS; at most MAX_PROBES live checks and ADDS_PER_WEEK adds per
round/week; autosources.MAX_ACTIVE caps auto-added boards overall.

Config (sources.yaml, all optional):

    search_discovery:
      queries: ["software engineer intern Canada", ...]
      domains: [jobs.ashbyhq.com, ...]

State (``SeenStore.insights["search_discovery"]``): ``last_run`` and
``checked`` ({board key: date last checked and not added}).
"""

import logging
import os
from datetime import UTC, datetime, timedelta

import requests

from scraper import autosources, discovery, filters
from scraper.adapters import get_adapter
from scraper.models import Job
from scraper.notify import display_company
from scraper.store import SeenStore

log = logging.getLogger(__name__)

API_URL = "https://api.tavily.com/search"
KEY_ENV = "TAVILY_API_KEY"
TIMEOUT_SECONDS = 30
RUN_EVERY_HOURS = 24
RECHECK_DAYS = 14
MAX_PROBES = 8
ADDS_PER_WEEK = 5
RESULTS_PER_QUERY = 20  # Tavily's maximum

DEFAULT_QUERIES = [
    "software engineer intern Canada",
    "software developer intern Toronto",
    "software engineering intern Vancouver",
    "software engineer intern 2027 United States",
    "new grad software engineer Canada",
    "new grad software engineer United States",
    "cloud engineer intern",
]
# Job-board hosts Hunter has an adapter for (see discovery.board_for).
DEFAULT_DOMAINS = [
    "jobs.ashbyhq.com", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
    "apply.workable.com", "jobs.smartrecruiters.com", "ats.rippling.com", "bamboohr.com",
    "myworkdayjobs.com",
]


def _key() -> str | None:
    return os.environ.get(KEY_ENV, "").strip() or None


def _section(store: SeenStore) -> dict:
    return store.insights.setdefault("search_discovery", {})


def due(store: SeenStore, now: datetime) -> bool:
    last = store.insights.get("search_discovery", {}).get("last_run")
    return not last or now - datetime.fromisoformat(last) >= timedelta(hours=RUN_EVERY_HOURS)


def run(store: SeenStore, config: dict, now: datetime | None = None) -> int:
    """One daily round; returns how many boards were added. ``config`` is the
    merged one (sources.yaml + auto-added), so known boards are skipped."""
    now = now or datetime.now(UTC)
    if _key() is None or not due(store, now):
        return 0
    section = _section(store)
    settings = config.get("search_discovery") or {}
    queries = settings.get("queries") or DEFAULT_QUERIES
    domains = settings.get("domains") or DEFAULT_DOMAINS

    hits = _boards_from_search(queries, domains)
    checked = section.setdefault("checked", {})
    recheck_after = (now - timedelta(days=RECHECK_DAYS)).date().isoformat()
    for key in [k for k, day in checked.items() if day < recheck_after]:
        del checked[key]
    skip = (discovery._configured(config) | discovery._ignored(config)
            | set(autosources.entries(store)) | set(checked))
    ranked = sorted(((k, v) for k, v in hits.items() if k not in skip),
                    key=lambda item: -item[1]["hits"])

    week_ago = (now - timedelta(days=7)).isoformat()
    entries = autosources.entries(store)
    added_this_week = sum(e.get("how") == "search" and e.get("added", "") >= week_ago
                          for e in entries.values())
    active = sum(e.get("status") == "active" for e in entries.values())
    room = min(ADDS_PER_WEEK - added_this_week, autosources.MAX_ACTIVE - active)
    predicates = filters.build_predicates(config.get("filters") or {})
    stamp = now.isoformat(timespec="seconds")
    added = 0
    for key, hit in ranked[:MAX_PROBES]:
        if added >= room:
            break
        matching = _matching_open_jobs(hit["board"], predicates)
        if matching:
            autosources.activate(store, key, hit["board"], hit["name"], matching, "search", stamp)
            added += 1
        else:
            checked[key] = now.date().isoformat()
    section["last_run"] = stamp
    log.info("Search discovery: %d boards in results, %d new, %d added.",
             len(hits), len(ranked), added)
    return added


def _boards_from_search(queries: list[str], domains: list[str]) -> dict[str, dict]:
    """{board key: {"board", "name", "hits"}} across every query's results."""
    boards: dict[str, dict] = {}
    for query in queries:
        try:
            results = _search(query, domains)
        except requests.RequestException as exc:
            log.warning("Search discovery: query %r failed (%s); skipping it.", query, exc)
            continue
        for result in results:
            board = discovery.board_for(Job(
                id="search", title=result.get("title", ""), company="", location="",
                url=result.get("url", ""), posted_at=None, description="", source="search",
            ))
            if board is None or "gh_jid" in board:
                continue
            # Built from a URL alone, a board can lack the company label it
            # runs and is shown under; fall back to the board's own id.
            if board["type"] not in ("tiktok", "amazon", "atlassian") and not board.get("company"):
                board["company"] = board.get("tenant") or discovery.board_key(board).split("/")[-1]
            key = discovery.board_key(board)
            entry = boards.setdefault(key, {"board": board, "hits": 0, "name": _name(board)})
            entry["hits"] += 1
    return boards


def _search(query: str, domains: list[str]) -> list[dict]:
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {_key()}"},
        json={"query": query, "include_domains": domains, "max_results": RESULTS_PER_QUERY,
              "search_depth": "basic", "time_range": "month"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json().get("results") or []


def _name(board: dict) -> str:
    raw = board.get("company") or board.get("tenant") or board.get("type", "")
    return display_company(raw)


def _matching_open_jobs(board: dict, predicates) -> int:
    """How many of the board's open jobs pass your filters; 0 if it errors."""
    try:
        jobs = get_adapter(board["type"])(board)
    except Exception as exc:  # a board that doesn't fetch is simply not added
        log.info("Search discovery: %s didn't fetch (%s).", discovery.board_key(board), exc)
        return 0
    return sum(filters.keep(job, predicates) for job in jobs)
