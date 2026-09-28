"""Knowledge that only exists across runs: when postings close, whether a
role was posted before, and when a company's hiring season opens.

Everything here reads and writes ``SeenStore.insights`` plus the ``closed``
field on store entries; nothing touches the dedup decision, so a bug here
can mislabel a message but can never suppress or duplicate one.

Closure detection is the delicate part. A job "closed" when a source that
fetched cleanly this run no longer lists it - but three things make a
missing job look closed when it isn't:

- a failed fetch (skipped: errors > 0),
- a capped fetch (Workday/Oracle/Microsoft stop at the newest
  MAX_POSTINGS): only jobs first seen after the oldest posting the fetch
  still reached are judged, since anything older may simply have scrolled
  out of the window; a capped fetch from an adapter that isn't
  newest-first (SmartRecruiters, TikTok) is skipped outright,
- a partial response (skipped when most tracked postings vanish at once).

Only jobs first seen after tracking began are followed, because only those
have a trustworthy start date for lifetime stats.
"""

import logging
import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from scraper import filters
from scraper.models import Job, JobNotes
from scraper.notify import categorize, display_company
from scraper.store import SeenStore

log = logging.getLogger(__name__)

REPOST_WINDOW_DAYS = 60
LIFETIME_SAMPLES = 50  # per source, most recent first-in-first-out
LIFETIME_MIN_SAMPLES = 5  # fewer than this and a "typical" is just noise
# A capped fetch reaching back to date D covers postings from D onward; a
# job is first seen on or after its posting date, so this margin keeps
# jobs seen right at the window's edge from being misjudged.
WINDOW_MARGIN_DAYS = 3
# More than this share of a source's tracked postings vanishing in one run
# looks like a truncated response, not a hiring freeze.
PARTIAL_FETCH_RATIO = 0.5
PARTIAL_FETCH_MIN_TRACKED = 4

# Aggregator feeds list hundreds of companies nobody chose to watch; season
# alerts and lifetime stats are only meaningful for the watchlist.
_AGGREGATOR_PREFIX = "github/"
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
SEASON_CATEGORIES = ("internship", "new_grad")


def _section(store: SeenStore, name: str) -> dict:
    return store.insights.setdefault(name, {})


def _tracking_since(store: SeenStore, now: datetime) -> datetime:
    since = store.insights.setdefault("since", now.isoformat(timespec="seconds"))
    return datetime.fromisoformat(since)


def _prefix(job_id: str) -> str:
    return job_id.rsplit(":", 1)[0]


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _role_key(job: Job) -> str:
    return f"{display_company(job.company).casefold()}|{' '.join(job.title.split()).casefold()}"


def _is_aggregated(job_or_source: Job | str) -> bool:
    source = job_or_source if isinstance(job_or_source, str) else job_or_source.source
    return source.startswith(_AGGREGATOR_PREFIX)


# --- closures ---------------------------------------------------------------


def find_closures(
    store: SeenStore, jobs: list[Job], stats: dict[str, dict], now: datetime | None = None
) -> list[tuple[str, str]]:
    """Return (job_id, source) for tracked postings that disappeared this
    run. Postings that reappeared after being marked closed are reopened."""
    now = now or datetime.now(UTC)
    since = _tracking_since(store, now)
    current = {job.id for job in jobs}
    for job in jobs:
        if store.has(job.id) and store.closed_on(job.id):
            store.reopen(job.id)

    by_source: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_source[job.source].append(job)

    # prefix -> (source, earliest first-seen time this fetch can vouch for)
    observed: dict[str, tuple[str, datetime | None]] = {}
    for source, group in by_source.items():
        stat = stats.get(source, {})
        if stat.get("errors"):
            continue
        covered_from = None
        if stat.get("unordered"):
            continue  # an arbitrary capped slice: absence proves nothing
        if stat.get("truncated"):
            dated = [d for job in group if (d := _parse(job.posted_at))]
            if not dated:
                continue
            covered_from = min(dated) + timedelta(days=WINDOW_MARGIN_DAYS)
        for job in group:
            observed[_prefix(job.id)] = (source, covered_from)

    tracked: dict[str, list[str]] = defaultdict(list)
    for job_id in store.ids():
        prefix = _prefix(job_id)
        if prefix not in observed or store.closed_on(job_id):
            continue
        seen = store.seen_at(job_id)
        covered_from = observed[prefix][1]
        if seen >= since and (covered_from is None or seen >= covered_from):
            tracked[prefix].append(job_id)

    closed: list[tuple[str, str]] = []
    for prefix, ids in tracked.items():
        source = observed[prefix][0]
        gone = [job_id for job_id in ids if job_id not in current]
        if len(ids) >= PARTIAL_FETCH_MIN_TRACKED and len(gone) > PARTIAL_FETCH_RATIO * len(ids):
            log.warning(
                "%s: %d of %d tracked postings vanished at once; treating as a partial "
                "fetch, not closures.", source, len(gone), len(ids),
            )
            continue
        closed.extend((job_id, source) for job_id in gone)
    return closed


def record_closure(
    store: SeenStore, job_id: str, source: str, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    store.mark_closed(job_id, now.date().isoformat())
    if _is_aggregated(source):
        return
    days = max((now - store.seen_at(job_id)).days, 0)
    samples = _section(store, "lifetimes").setdefault(source, [])
    samples.append(days)
    del samples[:-LIFETIME_SAMPLES]


def typical_open_days(store: SeenStore, source: str) -> int | None:
    samples = store.insights.get("lifetimes", {}).get(source, [])
    if len(samples) < LIFETIME_MIN_SAMPLES:
        return None
    return round(statistics.median(samples))


# --- starred jobs (set by the ⭐ button via scraper.feedback) --------------


def star(store: SeenStore, job: Job) -> None:
    _section(store, "starred")[job.id] = {
        "title": job.title,
        "company": job.company,
        "url": job.url,
    }


def unstar(store: SeenStore, job_id: str) -> None:
    store.insights.get("starred", {}).pop(job_id, None)


def starred(store: SeenStore) -> dict[str, dict]:
    return store.insights.get("starred", {})


# --- reposts and per-job notes ----------------------------------------------


def notes_for(jobs: list[Job], store: SeenStore) -> dict[str, JobNotes]:
    """JobNotes for each job that has something worth saying.

    A repost is the same company and title under a new id, where the
    earlier posting has since closed - i.e. the role went unfilled. An
    earlier posting that is still open is just a parallel req, which big
    employers open by the dozen, so it isn't flagged."""
    roles = store.insights.get("roles", {})
    notes = {}
    for job in jobs:
        reposted_since = None
        role = roles.get(_role_key(job))
        if role and role["id"] != job.id and store.has(role["id"]) and store.closed_on(role["id"]):
            reposted_since = role["first"][:10]
        typical = None if _is_aggregated(job) else typical_open_days(store, job.source)
        if reposted_since or typical is not None:
            notes[job.id] = JobNotes(reposted_since=reposted_since, typical_open_days=typical)
    return notes


def remember_roles(jobs: list[Job], store: SeenStore, now: datetime | None = None) -> None:
    """Index notified jobs by company+title so a later reappearance can be
    recognised as a repost. Only notified jobs are indexed: filtered-out
    roles would never be shown, so indexing them just grows the state file."""
    now_iso = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    roles = _section(store, "roles")
    for job in jobs:
        role = roles.setdefault(_role_key(job), {"first": now_iso})
        role["id"] = job.id
        role["last"] = now_iso


# --- hiring seasons ---------------------------------------------------------


def cycle(job: Job, now: datetime | None = None) -> int:
    """The recruiting cycle a posting belongs to: a year in the title wins
    ("Summer 2027"); otherwise postings from July onward count toward next
    year's cycle, which is when that season's roles open."""
    if match := _YEAR_RE.search(job.title):
        return int(match.group(1))
    when = _parse(job.posted_at) or now or datetime.now(UTC)
    return when.year + 1 if when.month >= 7 else when.year


def _season_candidates(jobs: list[Job], now: datetime | None) -> list[tuple[Job, str, int]]:
    out = []
    for job in jobs:
        category = categorize(job)
        if category in SEASON_CATEGORIES and not _is_aggregated(job):
            out.append((job, category, cycle(job, now)))
    return out


def _recorded(store: SeenStore, job: Job, category: str) -> int:
    company = display_company(job.company).casefold()
    return store.insights.get("seasons", {}).get(company, {}).get(category, 0)


def record_season(store: SeenStore, job: Job, category: str, year: int) -> None:
    company = display_company(job.company).casefold()
    seasons = _section(store, "seasons").setdefault(company, {})
    seasons[category] = max(seasons.get(category, 0), year)


def observe_seasons(
    jobs: list[Job], store: SeenStore, filters_config: dict, now: datetime | None = None
) -> None:
    """Silently record seasons from postings that are not being announced
    (already seen, or seeded from a new source). This is what keeps the
    first run after deploy - and every newly added source - from firing an
    alert per company. Only jobs passing the filters count, so an opened
    marketing internship doesn't pre-empt the engineering one."""
    predicates = filters.build_predicates(filters_config)
    kept = [job for job in jobs if filters.keep(job, predicates)]
    for job, category, year in _season_candidates(kept, now):
        record_season(store, job, category, year)


def season_openings(
    jobs: list[Job], store: SeenStore, now: datetime | None = None
) -> list[tuple[Job, str, int]]:
    """(first job, category, cycle) for each company whose season opens
    with this batch - one per company/category, however many roles opened."""
    openings: dict[tuple[str, str], tuple[Job, str, int]] = {}
    for job, category, year in _season_candidates(jobs, now):
        if year <= _recorded(store, job, category):
            continue
        key = (display_company(job.company).casefold(), category)
        if key not in openings or year > openings[key][2]:
            openings[key] = (job, category, year)
    return list(openings.values())


# --- pruning ----------------------------------------------------------------


def prune(store: SeenStore, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=REPOST_WINDOW_DAYS)
    roles = store.insights.get("roles", {})
    for key in [k for k, role in roles.items() if (_parse(role.get("last")) or now) < cutoff]:
        del roles[key]
    stars = store.insights.get("starred", {})
    for job_id in [job_id for job_id in stars if not store.has(job_id)]:
        del stars[job_id]
