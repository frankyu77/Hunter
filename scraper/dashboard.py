"""Static dashboard, published to GitHub Pages.

Built from state plus the current run's fetch into one self-contained
``index.html`` (data embedded, no external requests), so the same file works
on Pages and opened straight from disk. Rebuilt at most every
DASHBOARD_EVERY_MINUTES: GitHub Pages rate-limits deployments, and the
cron runs far more often than the charts change.

The repo is public, and so is Pages on a free account, so the readable
part of the page is job data only. Votes, stars and applications
(``feedback.votes``) ship only as the private layer: rows built here and
handed straight to ``private.seal``, so plaintext never reaches the page.
Without a passphrase configured there is no private layer at all. Once
unlocked, the page can also send 👍/👎/✅ back (scraper.inbox).

Most of the 60-day store holds just ids and first-seen times, but that is
enough for hiring activity: an id's prefix names its board. Titles exist only
for jobs you were sent (``feedback.sent``, 90 days) and jobs open right now,
which is what the search covers - as two tabs that never overlap: "Sent to
you" (each marked still open / closed where known) and "Not sent" (open now,
never reported: mostly the backlog recorded silently when a source was
added). A sent job is matched by id and by company+title+location, so the
collapsed "×3" duplicates of a sent job don't resurface as "not sent".

Backlogs distort activity. A newly added source is seeded with everything
it has open on one day, and so is a source whose state was lost and rebuilt
(this happened to 13 sources at once on 2026-09-04). Neither is hiring, so a
source's first day, and any day far above its norm (BURST_FACTOR x its
median day, and over BURST_MIN jobs), are left out of the activity counts.

Local preview: ``python -m scraper.dashboard`` builds site/ from state alone
(no "open now" tab - that needs a fetch).
"""

import argparse
import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from scraper import filters, inbox, private
from scraper.models import Job
from scraper.notify import categorize, display_company
from scraper.store import SeenStore

log = logging.getLogger(__name__)

DASHBOARD_EVERY_MINUTES = 60
ACTIVITY_WEEKS = 8
ACTIVITY_SOURCES = 20
OPEN_LIMIT = 3000
LIFETIME_MIN_SAMPLES = 5
BURST_FACTOR = 10
BURST_MIN = 50
SITE_DIR = "site"


# --- state bookkeeping -------------------------------------------------------


def remember_sources(jobs: list[Job], store: SeenStore) -> None:
    """Map id prefixes to source labels, so activity can be labelled even for
    boards (Workday tenants) whose ids don't spell the company."""
    sources = store.insights.setdefault("sources", {})
    for job in jobs:
        sources[_prefix(job.id)] = job.source


def is_due(store: SeenStore, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    built = store.insights.get("dashboard_built_at")
    if not built:
        return True
    return now - datetime.fromisoformat(built) >= timedelta(minutes=DASHBOARD_EVERY_MINUTES)


def build(
    store: SeenStore,
    out_dir: str = SITE_DIR,
    open_jobs: list[Job] | None = None,
    filters_config: dict | None = None,
    now: datetime | None = None,
) -> Path:
    now = now or datetime.now(UTC)
    data = collect(store, open_jobs, filters_config or {}, now)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    page = out / "index.html"
    # "</" would close the <script> the data sits in.
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    # Sealed first: it is base64 and fixed keys, so it can't contain __DATA__,
    # whereas job data could contain the private placeholder.
    sealed = json.dumps(_sealed(store))
    page.write_text(TEMPLATE.replace("__PRIVATE__", sealed).replace("__DATA__", payload),
                    encoding="utf-8")
    store.insights["dashboard_built_at"] = now.isoformat(timespec="seconds")
    return page


# --- data --------------------------------------------------------------------


def collect(
    store: SeenStore, open_jobs: list[Job] | None, filters_config: dict, now: datetime
) -> dict:
    sources = store.insights.get("sources", {})
    snapshots = list(store.feedback.get("sent", {}).values())
    live = {job.id for job in open_jobs} if open_jobs is not None else None
    history = _history(store, snapshots, live)
    activity = _activity(store, sources, now)
    lifetimes = _lifetimes(store)
    pooled = [d for samples in store.insights.get("lifetimes", {}).values() for d in samples]
    week_ago = (now - timedelta(days=7)).date().isoformat()
    open_rows = (
        _not_sent(open_jobs, snapshots, filters_config) if open_jobs is not None else None
    )
    return {
        "generated": now.isoformat(timespec="seconds"),
        "stats": {
            "tracked": len(store),
            "new_week": activity.pop("last_7_days"),
            "sent_week": sum(row["d"] >= week_ago for row in history),
            "open_matching": len(open_rows) if open_rows is not None else None,
            "median_close": round(statistics.median(pooled)) if pooled else None,
        },
        "history": history,
        "open": open_rows,
        "activity": activity,
        "lifetimes": lifetimes,
        "inbox": _inbox(store),
    }


def _inbox(store: SeenStore) -> dict | None:
    """Where the page posts 👍/👎/✅ (see scraper.inbox): public, just an
    address. None until a run has created the inbox issue."""
    number = store.feedback.get("inbox", {}).get("issue")
    return {"repo": inbox.repo(), "issue": number} if number and inbox.repo() else None


def _sealed(store: SeenStore) -> dict | None:
    """The private layer, encrypted - or None (no layer) if no passphrase is
    set or sealing fails. A failure here costs the private layer for an
    hour, never the public page."""
    phrase = private.passphrase()
    if phrase is None:
        return None
    try:
        return private.seal({"mine": _mine(store)}, phrase)
    except Exception:
        log.exception("Sealing the private layer failed; publishing without it.")
        return None


def _mine(store: SeenStore) -> list[dict]:
    """Every job you voted on, starred or applied to, newest first. Shaped
    like a history row so the page renders it with the same code, plus the
    marks. Never pruned, so it outlives the 90-day sent history."""
    rows = []
    for record in store.feedback.get("votes", {}).values():
        row = _row(record, record.get("updated_at", "")[:10])
        row |= {
            "vote": record.get("vote"),
            "star": bool(record.get("starred")),
            "applied": (record.get("applied_at") or record.get("updated_at", ""))[:10]
            if record.get("applied") else "",
            # When each field last changed: the page drops a click it is
            # still showing as pending once this says the run has it.
            "set": record.get("set_at", {}),
        }
        rows.append(row)
    return sorted(rows, key=lambda row: row["d"], reverse=True)


def _history(store: SeenStore, snapshots: list[dict], live: set[str] | None) -> list[dict]:
    rows = []
    for snap in snapshots:
        row = _row(snap, snap.get("sent_at", "")[:10])
        row["st"] = _status(store, snap.get("id", ""), live)
        rows.append(row)
    return sorted(rows, key=lambda row: row["d"], reverse=True)


def _status(store: SeenStore, job_id: str, live: set[str] | None) -> str:
    """"open" if this run's fetch still lists it, "closed" if a closure was
    recorded, else "" - absent from a capped fetch proves nothing."""
    if live is not None and job_id in live:
        return "open"
    if store.has(job_id) and store.closed_on(job_id):
        return "closed"
    return ""


def _not_sent(jobs: list[Job], snapshots: list[dict], filters_config: dict) -> list[dict]:
    sent_ids = {snap.get("id") for snap in snapshots}
    sent_roles = {_role(snap.get("company", ""), snap.get("title", ""), snap.get("location", ""))
                  for snap in snapshots}
    predicates = filters.build_predicates(filters_config)
    kept = [
        job for job in jobs
        if filters.keep(job, predicates) and job.id not in sent_ids
        and _role(job.company, job.title, job.location) not in sent_roles
    ]
    kept.sort(key=lambda job: job.posted_at or "", reverse=True)
    return [
        _row({"id": j.id, "title": j.title, "company": j.company, "location": j.location,
              "url": j.url, "source": j.source, "category": categorize(j)},
             (j.posted_at or "")[:10])
        for j in kept[:OPEN_LIMIT]
    ]


def _role(company: str, title: str, location: str) -> tuple[str, str, str]:
    # Same identity notify uses to collapse duplicates into one "×N" entry.
    return (display_company(company).casefold(), " ".join(title.split()).casefold(),
            " ".join(location.split()).casefold())


def _row(snap: dict, day: str) -> dict:
    return {
        "i": snap.get("id", ""),
        "t": snap.get("title", ""),
        "c": display_company(snap.get("company", "")),
        "l": snap.get("location", ""),
        "u": snap.get("url", ""),
        "k": snap.get("category", ""),
        "s": _source_label(snap.get("source", "")),
        "d": day,
    }


def _activity(store: SeenStore, sources: dict, now: datetime) -> dict:
    """New postings per week per board over the last ACTIVITY_WEEKS weeks
    (Monday-start), busiest boards first."""
    this_monday = now.date() - timedelta(days=now.weekday())
    weeks = [this_monday - timedelta(weeks=n) for n in range(ACTIVITY_WEEKS - 1, -1, -1)]
    start = weeks[0]

    seen: dict[str, list[date]] = defaultdict(list)
    for job_id in store.ids():
        if when := store.first_seen(job_id):  # undatable entries are skipped
            seen[_prefix(job_id)].append(when.date())

    per_board: dict[str, Counter] = {}
    totals: Counter = Counter()
    week_ago = now.date() - timedelta(days=7)
    last_7_days = 0
    for prefix, days in seen.items():
        backlog = _backlog_days(Counter(days))
        counts: Counter = Counter()
        for day in days:
            if day in backlog or day < start:
                continue
            last_7_days += day > week_ago
            week = day - timedelta(days=day.weekday())
            counts[week] += 1
            totals[week] += 1
        if counts:
            label = _source_label(sources.get(prefix, prefix.replace(":", "/", 1)))
            per_board[label] = per_board.get(label, Counter()) + counts

    busiest = sorted(per_board.items(), key=lambda item: sum(item[1].values()), reverse=True)
    return {
        "last_7_days": last_7_days,
        "weeks": [week.isoformat() for week in weeks],
        "totals": [totals[week] for week in weeks],
        "rows": [
            {"label": label, "counts": [counts[week] for week in weeks]}
            for label, counts in busiest[:ACTIVITY_SOURCES]
        ],
    }


def _backlog_days(per_day: Counter) -> set[date]:
    """Days that look like a backlog load rather than new postings."""
    typical = statistics.median(per_day.values())
    bursts = {day for day, n in per_day.items() if n > max(BURST_MIN, BURST_FACTOR * typical)}
    return bursts | {min(per_day)}


def _lifetimes(store: SeenStore) -> list[dict]:
    rows = [
        {"label": _source_label(source), "median": round(statistics.median(samples)),
         "n": len(samples)}
        for source, samples in store.insights.get("lifetimes", {}).items()
        if len(samples) >= LIFETIME_MIN_SAMPLES
    ]
    return sorted(rows, key=lambda row: (row["median"], row["label"]))


def _prefix(job_id: str) -> str:
    return job_id.rsplit(":", 1)[0]


def _source_label(source: str) -> str:
    """"workday/nvidia" -> "NVIDIA"; "github/SimplifyJobs/New-Grad-Positions"
    -> "New Grad Positions feed"."""
    kind, _, name = source.partition("/")
    if kind == "github":
        return f"{name.rsplit('/', 1)[-1].replace('-', ' ')} feed"
    if kind in ("tiktok", "amazon") or not name:
        return display_company(kind)
    return display_company(name)


# --- page ---------------------------------------------------------------------

TEMPLATE = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper.dashboard",
                                     description="Build the dashboard from state alone.")
    parser.add_argument("--store", default="seen_jobs.json")
    parser.add_argument("--out", default=SITE_DIR)
    args = parser.parse_args(argv)
    page = build(SeenStore(args.store), args.out)
    print(f"Wrote {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
