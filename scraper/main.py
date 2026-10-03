"""Pipeline orchestration: FETCH -> NORMALIZE -> DEDUP -> FILTER -> NOTIFY.

Each stage is a separate function so later stages can fill them in without
rewiring, and so dry runs and tests can exercise the seams. Runs are
stateless: state is loaded from the SeenStore at the start and saved at the
end.
"""

import argparse
import html
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import date

import requests
import yaml

from scraper import alerts, dashboard, discovery, feedback, filters, health, inbox, insights
from scraper import notify as telegram
from scraper.adapters import get_adapter, max_postings, newest_first
from scraper.models import Job, JobNotes
from scraper.store import SeenStore

log = logging.getLogger("scraper")

# Waits between retry attempts on timeouts and 5xx. Per-source and small so
# the whole run still finishes quickly even with a flaky source.
RETRY_WAITS = (1, 4, 16)
ORDER_SLACK_DAYS = 1
PRUNE_MAX_AGE_DAYS = 60


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("Config file %s not found; running with no sources.", path)
        return {}
    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(config).__name__}")
    return config


def fetch_all(sources: list[dict]) -> tuple[list[Job], dict[str, dict]]:
    """Fetch every source, each inside its own try/except (bulkhead):
    one broken source must never sink the run. Returns the jobs plus
    per-source stats for the run summary, health and closure tracking.

    ``truncated`` marks a fetch that hit its adapter's MAX_POSTINGS cap, so
    closure tracking knows the list is only the newest slice; ``unordered``
    marks a capped fetch that isn't newest-first, where absence proves
    nothing."""
    jobs: list[Job] = []
    stats: dict[str, dict] = {}
    for source in sources:
        name = source.get("company") or source.get("repo") or source.get("name") or "?"
        label = f"{source.get('type', '?')}/{name}"
        stat = stats.setdefault(label, {"fetched": 0, "errors": 0})
        try:
            fetch = get_adapter(source["type"])
            fetched = fetch_with_retry(fetch, source, label)
            stat["fetched"] += len(fetched)
            cap = max_postings(fetch)
            stat["truncated"] = cap is not None and len(fetched) >= cap
            if stat["truncated"] and not (newest_first(fetch) and _dated_newest_first(fetched)):
                stat["unordered"] = True
            log.info("%s: fetched %d jobs", label, len(fetched))
            jobs.extend(fetched)
        except Exception:
            stat["errors"] += 1
            log.exception("%s: fetch failed; continuing with remaining sources", label)
    return jobs, stats


def _dated_newest_first(jobs: list[Job]) -> bool:
    """Whether this fetch actually came back newest-first. An adapter can
    promise it and a board still break it (TD's Workday interleaves), so a
    capped slice is only trusted as a recency window when its dates agree.

    Undated postings are ignored, and a posting may be up to
    ORDER_SLACK_DAYS newer than the oldest seen before it: Workday's
    "Posted Today"/"Yesterday" labels straddle time zones, so NVIDIA's
    otherwise sorted list has a few one-day hiccups. TD's jumps are days."""
    oldest = None
    for job in jobs:
        if not job.posted_at:
            continue
        try:
            day = date.fromisoformat(job.posted_at[:10])
        except ValueError:
            continue
        if oldest is not None and (day - oldest).days > ORDER_SLACK_DAYS:
            return False
        oldest = day if oldest is None else min(oldest, day)
    return True


def fetch_with_retry(fetch: Callable, config: dict, label: str) -> list[Job]:
    """Retry timeouts, 5xx and 429 with exponential backoff. Other 4xx fail
    fast: they mean the source config is wrong, and retrying won't fix that."""
    attempts = len(RETRY_WAITS) + 1
    for attempt in range(attempts):
        try:
            return fetch(config)
        except Exception as exc:
            if attempt == attempts - 1 or not _retryable(exc):
                raise
            wait = RETRY_WAITS[attempt]
            log.warning(
                "%s: attempt %d/%d failed (%s); retrying in %ds",
                label, attempt + 1, attempts, exc, wait,
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


def _retryable(exc: Exception) -> bool:
    # 429 is the one 4xx that isn't a config error: the source is asking us to
    # slow down, and backing off is exactly the fix (Microsoft does this).
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is not None and (
            response.status_code >= 500 or response.status_code == 429
        )
    return isinstance(exc, requests.exceptions.ConnectionError | requests.exceptions.Timeout)


def normalize(jobs: list[Job]) -> list[Job]:
    # Adapters already return normalized Job objects; this seam exists for
    # any cross-source cleanup that turns out to be needed later.
    return jobs


def dedup(jobs: list[Job], store: SeenStore) -> list[Job]:
    return [job for job in jobs if not store.has(job.id)]


def apply_filters(jobs: list[Job], filters_config: dict) -> list[Job]:
    predicates = filters.build_predicates(filters_config)
    if not predicates:
        return jobs
    kept = [job for job in jobs if filters.keep(job, predicates)]
    if len(kept) != len(jobs):
        log.info("Filters dropped %d of %d new jobs.", len(jobs) - len(kept), len(jobs))
    return kept


def notify(
    jobs: list[Job],
    store: SeenStore,
    dry_run: bool,
    digest_threshold: int,
    notes: dict[str, JobNotes] | None = None,
    school: str | None = None,
) -> list[Job]:
    """Send each job (or one digest), returning the jobs actually sent.
    A job whose send failed is NOT recorded as seen, so it retries next
    run - never-miss beats never-duplicate."""
    if not jobs:
        return []

    # Identical-looking postings (one role opened as several reqs) share one
    # entry, so the digest threshold counts what the reader will actually see.
    groups = telegram.group_duplicates(jobs)

    if len(groups) > digest_threshold:
        # Digest mode: one summary message instead of flooding the chat.
        if dry_run:
            print(f"DIGEST of {len(jobs)} new jobs:")
            for dupes in groups:
                job = dupes[0]
                print(f"  - {job.title} @ {job.company} ({job.location}){_times(dupes)}")
        else:
            try:
                telegram.send_digest(jobs, notes)
            except Exception:
                log.exception("Digest send failed; jobs stay unseen and retry next run.")
                return []
        for job in jobs:
            store.add(job)
        feedback.remember_sent([dupes[0] for dupes in groups], store)
        return jobs

    sent = []
    for dupes in groups:
        job = dupes[0]
        try:
            if dry_run:
                print(
                    f"NEW: {job.title} @ {job.company} ({job.location}){_times(dupes)}"
                    f" -> {job.url}"
                )
            else:
                note = next((notes[d.id] for d in dupes if notes and d.id in notes), None)
                telegram.send(job, copies=len(dupes), notes=note, school=school)
        except Exception:
            log.exception("Send failed for %s; it stays unseen and retries next run.", job.id)
            continue
        # Record only after the message is out: a crash in between re-sends a
        # harmless duplicate, while the reverse order would miss a job. Every
        # collapsed duplicate was covered by this one message.
        for dupe in dupes:
            store.add(dupe)
            sent.append(dupe)
        feedback.remember_sent([job], store)
    return sent


def _times(dupes: list[Job]) -> str:
    return f" x{len(dupes)}" if len(dupes) > 1 else ""


def seed_new_sources(
    fresh: list[Job], normalized: list[Job], store: SeenStore
) -> list[Job]:
    """Silently seed sources that are new to the watchlist, returning the
    remaining genuinely-new jobs.

    A source with no previously-seen jobs at all was just added (or fetched
    successfully for the first time); alerting would replay its entire
    backlog. Recognizing sources by "has at least one seen job" (rather than
    by health history) keeps the never-miss rule intact for existing sources
    recovering from an outage."""
    seen_sources = {job.source for job in normalized if store.has(job.id)}
    backlog = [job for job in fresh if job.source not in seen_sources]
    if not backlog:
        return fresh
    for job in backlog:
        store.add(job)
    for label, count in sorted(Counter(job.source for job in backlog).items()):
        log.info("%s: new source; seeded %d existing postings silently.", label, count)
    return [job for job in fresh if job.source in seen_sources]


def seed(jobs: list[Job], store: SeenStore) -> None:
    """Silent first-run seeding: an empty store means this is the first run
    ever, so record everything currently posted without notifying. Without
    this, run one would fire a message for every existing posting."""
    for job in jobs:
        store.add(job)
    store.save()
    log.info("First run: seeded %d current postings without notifying.", len(jobs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scraper.main",
        description="Poll job sources and notify about never-seen-before postings.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print would-be notifications to stdout instead of sending to Telegram",
    )
    parser.add_argument("--config", default="sources.yaml", help="path to the sources YAML file")
    parser.add_argument(
        "--store", default="seen_jobs.json", help="path to the seen-jobs state file"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    config = load_config(args.config)
    sources = config.get("sources") or []
    store = SeenStore(args.store)
    if not args.dry_run:
        read_button_presses(store)

    fetched, stats = fetch_all(sources)
    normalized = normalize(fetched)
    warnings = health.record_run(store.health, stats)
    acted = 0 if args.dry_run else read_dashboard_actions(store, normalized)
    announce_alert_settings(store, args.dry_run)

    if normalized and len(store) == 0:
        seed(normalized, store)
        return 0

    filters_config = config.get("filters") or {}
    dashboard.remember_sources(normalized, store)
    # Closures are judged against the full fetch, before dedup narrows it.
    closures = insights.find_closures(store, normalized, stats)
    fresh = dedup(normalized, store)
    fresh = seed_new_sources(fresh, normalized, store)
    matched = apply_filters(fresh, filters_config)
    discovery.observe(fresh, matched, store, config)
    # Of those, what pings Telegram: the alert settings chosen on the
    # dashboard. The rest stay on the dashboard and are recorded below.
    matched = alerts.select(matched, store)

    # Seasons: everything not being announced this run is recorded silently
    # first, so only a genuinely new cycle can trigger an alert.
    matched_ids = {job.id for job in matched}
    insights.observe_seasons(
        [job for job in normalized if job.id not in matched_ids], store, filters_config
    )
    announce_seasons(insights.season_openings(matched, store), store, args.dry_run)

    notes = insights.notes_for(matched, store)
    sent = notify(
        matched,
        store,
        args.dry_run,
        digest_threshold=filters_config.get("digest_threshold", 10),
        notes=notes,
        school=(config.get("profile") or {}).get("school"),
    )
    insights.remember_roles(sent, store)
    # Record filtered-out jobs as seen too (after notify, so a crash can't
    # mark a matched job seen before its message went out). Otherwise every
    # filtered job re-enters the diff as "new" on every run forever. Jobs
    # whose send failed are deliberately left unseen so they retry.
    for job in fresh:
        if job.id not in matched_ids and not store.has(job.id):
            store.add(job)

    record_closures(closures, store)
    announce_discovery(store, config, args.dry_run)
    announce_summary(store, args.dry_run)

    # Prune at the very end, after notifications and state updates, so it
    # can never race the dedup.
    store.prune(PRUNE_MAX_AGE_DAYS)
    insights.prune(store)
    feedback.prune(store)
    build_dashboard(store, normalized, filters_config, now=acted > 0)
    store.save()

    for message in warnings:
        try:
            if args.dry_run:
                print(f"WARNING: {message}")
            else:
                telegram.send_text(f"Health warning: {message}")
        except Exception:
            log.exception("Failed to send health warning.")

    summarize(stats, fresh, matched, sent)
    log.info(
        "Run complete: %d sources, %d fetched, %d new jobs, %d notified.",
        len(sources),
        len(fetched),
        len(fresh),
        len(sent),
    )
    return 0


def read_button_presses(store: SeenStore) -> None:
    """Apply Telegram ✅ presses (and 👍/👎 on older messages) since the
    last run. Skipped on dry runs: reading would consume the presses. A failure here
    never sinks the run - unread presses stay queued at Telegram."""
    try:
        count = feedback.process_updates(store)
    except Exception:
        log.exception("Reading button presses failed; they stay queued for the next run.")
        return
    if count:
        log.info("Read %d button press update(s).", count)


def read_dashboard_actions(store: SeenStore, jobs: list[Job]) -> int:
    """Apply 👍/👎/✅ clicked on the dashboard; return how many. After the
    fetch, so a job voted on from the "Not sent" tab gets its full snapshot.
    Skipped on dry runs, like button presses; a failure leaves the actions
    in the inbox for the next run."""
    try:
        count = inbox.process(store, jobs)
    except Exception:
        log.exception("Reading dashboard actions failed; they stay in the inbox.")
        return 0
    if count:
        log.info("Applied %d dashboard action(s).", count)
    return count


def announce_alert_settings(store: SeenStore, dry_run: bool) -> None:
    """Confirm in Telegram that alert settings changed on the dashboard took
    effect. The flag clears only once sent, so a failed send retries."""
    current = alerts.settings(store)
    if not current.get("announce"):
        return
    text = f"🔔 <b>Alert settings updated</b>\n{html.escape(alerts.describe(current))}"
    try:
        if dry_run:
            print(f"ALERTS: {alerts.describe(current)}")
        else:
            telegram.send_html(text)
    except Exception:
        log.exception("Alert-settings confirmation failed; retrying next run.")
        return
    current.pop("announce", None)


def build_dashboard(
    store: SeenStore, jobs: list[Job], filters_config: dict, now: bool = False
) -> None:
    """Hourly, or ``now`` when this run applied dashboard clicks - so the
    page you clicked on reflects them in a few minutes, not an hour. Writes
    site/ for the workflow to publish to GitHub Pages. Runs after pruning so
    it shows exactly the state being saved; a failure only costs this
    refresh. (Pages' 10-builds-an-hour soft limit doesn't apply to sites
    deployed by an Actions workflow, which this is.)"""
    if not (now or dashboard.is_due(store)):
        return
    try:
        page = dashboard.build(store, dashboard.SITE_DIR, jobs, filters_config)
    except Exception:
        log.exception("Dashboard build failed; the published site stays as it was.")
        return
    log.info("Dashboard written to %s.", page)


def announce_summary(store: SeenStore, dry_run: bool) -> None:
    """Weekly, privately: your funnel (sent -> 👍/👎 -> ✅). Marked done only
    once sent, so a failed send retries next run."""
    try:
        summary = feedback.weekly_summary(store)
        if summary is None:
            return
        if dry_run:
            print(f"SUMMARY:\n{summary}")
        else:
            telegram.send_html(summary)
    except Exception:
        log.exception("Weekly summary failed; retrying next run.")
        return
    feedback.mark_summarised(store)


def announce_discovery(store: SeenStore, config: dict, dry_run: bool) -> None:
    """Weekly: suggest aggregator-only companies to poll directly. The tally
    resets only once the report is out, so a failed send retries next run."""
    try:
        report = discovery.due_report(store, config)
        if report is None:
            if discovery.is_due(store):
                discovery.reset(store)  # due, but nothing qualified this week
            return
        if dry_run:
            print(f"DISCOVERY:\n{report}")
        else:
            telegram.send_html(report)
    except Exception:
        log.exception("Source discovery report failed; retrying next run.")
        return
    discovery.reset(store)


def announce_seasons(openings: list, store: SeenStore, dry_run: bool) -> None:
    """Send one loud message per company whose hiring season just opened.
    The season is recorded only after its alert is out; a failed send just
    lets the next posting from that company try again."""
    for job, category, year in openings:
        try:
            if dry_run:
                print(f"SEASON: {job.company} opened {category} {year} ({job.title})")
            else:
                telegram.send_html(telegram.format_season_alert(job, category, year))
        except Exception:
            log.exception("Season alert failed for %s; will retry on its next posting.", job.id)
            continue
        insights.record_season(store, job, category, year)


def record_closures(closures: list[tuple[str, str]], store: SeenStore) -> None:
    """Record every closure - it feeds how long postings stay open. No
    alerts: those were for starred jobs, and stars were removed."""
    for job_id, source in closures:
        insights.record_closure(store, job_id, source)
    if closures:
        log.info("Detected %d closed postings.", len(closures))


def summarize(stats: dict, fresh: list[Job], matched: list[Job], sent: list[Job]) -> None:
    """One readable line per source - the primary observability surface in
    the Actions logs."""
    new_by = Counter(job.source for job in fresh)
    matched_by = Counter(job.source for job in matched)
    sent_by = Counter(job.source for job in sent)
    for label in sorted(stats):
        stat = stats[label]
        log.info(
            "%s: fetched=%d new=%d filtered_out=%d notified=%d errors=%d",
            label,
            stat["fetched"],
            new_by[label],
            new_by[label] - matched_by[label],
            sent_by[label],
            stat["errors"],
        )


if __name__ == "__main__":
    sys.exit(main())
