"""Auto-added sources: company boards Hunter starts polling on its own.

scraper.discovery tallies which company boards keep feeding you jobs late
through the aggregator feeds (SimplifyJobs and co. list a job hours to days
after the company posts it). Once a board has fed MIN_MATCHED matching jobs
and passes a live fetch with the real adapter, it is polled directly from
then on - no sources.yaml edit, no commit. The dashboard's Sources page shows
them, with Remove (never suggest again) and Resume, and lets you Add any
other suggestion; those clicks arrive through the inbox like votes.

They live in state, not sources.yaml, and are merged into the config at the
start of each run (``merged_config``). Everything downstream then treats
them like hand-written sources: fetch bulkheads, health counters, silent
seeding of a new source's backlog (so adding a board never floods the chat),
and discovery's "already polled" check, so the weekly report stops
suggesting them.

Guardrails, because each board adds a fetch to every run: at most
ADDS_PER_WEEK automatic adds a week, MAX_ACTIVE in total, checked at most
once a day (each check probes up to discovery.MAX_PROBES boards), and a board
that fails PAUSE_AFTER_FAILURES runs in a row is paused until you resume it.

State (``SeenStore.insights["auto_sources"]``), keyed by board key:

    board     the source entry, as sources.yaml would hold it
    name      company name for display
    status    active / paused / removed / requested (Add clicked, not yet
              verified) / failed (Add clicked, didn't verify)
    added     when it was first polled; how: "auto" or "dashboard"
    matched   matching jobs it had fed through the feeds when added
    set_at    when the dashboard last changed it (latest click wins)
    announce  a Telegram message is owed (cleared once sent)

``insights["auto_sources_checked"]`` is when the daily auto-add check last ran.
"""

import copy
import html
import logging
import re
from datetime import UTC, datetime, timedelta

from scraper import discovery
from scraper.store import SeenStore

log = logging.getLogger(__name__)

MIN_MATCHED = 3
ADDS_PER_WEEK = 3
MAX_ACTIVE = 25
CHECK_EVERY_HOURS = 24
PAUSE_AFTER_FAILURES = 5
ACTIONS = ("add", "remove", "resume")
_KEY = re.compile(r"[\w@.:/-]{1,200}")


def _section(store: SeenStore) -> dict:
    return store.insights.setdefault("auto_sources", {})


def entries(store: SeenStore) -> dict[str, dict]:
    return store.insights.get("auto_sources", {})


def label(source: dict) -> str:
    """The label a source runs under: health counters, logs, Job.source."""
    name = source.get("company") or source.get("repo") or source.get("name") or "?"
    return f"{source.get('type', '?')}/{name}"


def merged_config(config: dict, store: SeenStore) -> dict:
    """``config`` plus the active auto-added sources to poll. Paused and
    removed ones go on discovery's ignore list, so they aren't suggested
    (or auto-added) again; a requested Add stays visible to discovery so it
    can be verified."""
    merged = copy.deepcopy(config)
    merged["sources"] = list(merged.get("sources") or [])
    merged["discovery"] = dict(merged.get("discovery") or {})
    merged["discovery"]["ignore"] = list(merged["discovery"].get("ignore") or [])
    for key, entry in entries(store).items():
        if entry.get("status") == "active" and entry.get("board"):
            merged["sources"].append(dict(entry["board"]))
        elif entry.get("status") in ("paused", "removed"):
            merged["discovery"]["ignore"].append(key)
        # An embedded-Greenhouse board is tallied under the site it was seen
        # on; once added under its real key, that tally key is spoken for too.
        if entry.get("tally_key") and entry.get("status") != "requested":
            merged["discovery"]["ignore"].append(entry["tally_key"])
    return merged


# --- each run -------------------------------------------------------------------


def update(store: SeenStore, config: dict, now: datetime | None = None,
           probe=None, resolve=None) -> None:
    """Pause failing boards, verify dashboard Adds, and (daily) auto-add the
    boards that keep feeding you late jobs. ``config`` is the merged one."""
    now = now or datetime.now(UTC)
    section = _section(store)
    stamp = now.isoformat(timespec="seconds")

    for key, entry in section.items():
        failures = store.health.get(label(entry.get("board") or {}), {})
        if (entry.get("status") == "active"
                and failures.get("consecutive_failures", 0) >= PAUSE_AFTER_FAILURES):
            entry.update(status="paused", announce="paused")
            log.info("Auto source %s paused after %d failed runs.", key, PAUSE_AFTER_FAILURES)

    active = sum(e.get("status") == "active" for e in section.values())
    requested = {k for k, e in section.items() if e.get("status") == "requested"}
    if requested:
        # Clicked Add on the dashboard: verify those now, whatever the weekly cap.
        found = discovery.verified(store, config, len(requested), 1, probe, resolve,
                                   only=requested)
        for key, entry, _open in found:
            if active >= MAX_ACTIVE:
                break
            _activate(section, key, entry, "dashboard", stamp)
            active += 1
        for key in requested:
            if section.get(key, {}).get("status") == "requested":
                section[key].update(status="failed", announce="failed")
                log.info("Auto source %s requested but did not verify.", key)

    last = store.insights.get("auto_sources_checked")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=CHECK_EVERY_HOURS):
        return
    week_ago = (now - timedelta(days=7)).isoformat()
    added_this_week = sum(e.get("how") == "auto" and e.get("added", "") >= week_ago
                          for e in section.values())
    room = min(ADDS_PER_WEEK - added_this_week, MAX_ACTIVE - active)
    if room > 0:
        for key, entry, _open in discovery.verified(store, config, room, MIN_MATCHED,
                                                     probe, resolve):
            _activate(section, key, entry, "auto", stamp)
    store.insights["auto_sources_checked"] = stamp


def _activate(section: dict, key: str, entry: dict, how: str, stamp: str) -> None:
    # An embedded-Greenhouse board is tallied under the site it was seen on
    # and only gets its real key once resolved: carry the request across.
    tally_key = entry.get("tally_key")
    previous = section.pop(tally_key, {}) if tally_key and tally_key != key else {}
    current = section.get(key, previous)
    if tally_key and tally_key != key:
        current = current | {"tally_key": tally_key}
    section[key] = current | {
        "board": entry["board"], "name": entry["name"], "status": "active",
        "added": stamp, "how": how, "matched": entry["matched"], "announce": "added",
    }
    log.info("Auto source %s (%s) added: %d matching jobs came late via the feeds.",
             key, entry["name"], entry["matched"])


# --- dashboard clicks (via scraper.inbox) ----------------------------------------


def valid(key, value) -> bool:
    return isinstance(key, str) and bool(_KEY.fullmatch(key)) and value in ACTIONS


def request(store: SeenStore, key: str, action: str, clicked: datetime) -> bool:
    """Apply Add / Remove / Resume clicked on the Sources page."""
    section = _section(store)
    entry = section.get(key)
    if entry and entry.get("set_at") and datetime.fromisoformat(entry["set_at"]) > clicked:
        return False  # a newer click already won
    candidate = discovery._section(store).get("candidates", {}).get(key)
    if action == "add":
        if entry and entry.get("status") == "active":
            return False
        name = (entry or {}).get("name") or (candidate or {}).get("name") or key
        section[key] = (entry or {}) | {"status": "requested", "name": name}
    elif not entry:
        log.info("Dashboard asked to %s %s, which isn't an auto-added source.", action, key)
        return False
    elif action == "remove":
        entry["status"] = "removed"
    elif action == "resume" and entry.get("status") == "paused":
        entry["status"] = "active"
        store.health.pop(label(entry.get("board") or {}), None)  # a fresh start
    section[key]["set_at"] = clicked.isoformat(timespec="milliseconds")
    log.info("Dashboard: source %s -> %s", key, section[key]["status"])
    return True


# --- Telegram ---------------------------------------------------------------------


def announcements(store: SeenStore) -> list[tuple[str, str]]:
    """(key, message) for every change Telegram hasn't been told about."""
    e = html.escape
    out = []
    for key, entry in entries(store).items():
        kind = entry.get("announce")
        if not kind:
            continue
        name = e(entry.get("name", key))
        if kind == "added":
            why = (f"{entry['matched']} matching jobs reached you late through the "
                   "aggregator feeds. " if entry.get("how") == "auto" else "")
            text = (f"🧭 <b>Now polling {name} directly</b>\n{why}New postings now arrive "
                    "within minutes. Remove it on the dashboard's Sources page.")
        elif kind == "paused":
            text = (f"⏸ <b>Paused {name}</b>: its board failed {PAUSE_AFTER_FAILURES} runs in a "
                    "row. Resume it on the dashboard's Sources page.")
        else:
            text = (f"⚠️ <b>Couldn't add {name}</b>: its board didn't respond with any jobs. "
                    "It may have moved; try again later from the Sources page.")
        out.append((key, text))
    return out


def announced(store: SeenStore, key: str) -> None:
    entries(store).get(key, {}).pop("announce", None)


# --- dashboard ----------------------------------------------------------------------


def for_dashboard(store: SeenStore, config: dict, sent_sources: dict[str, int],
                  limit: int = 15) -> dict:
    """What the Sources page shows. Public data: which boards are polled is
    no more personal than sources.yaml, which is public too."""
    rows = []
    for key, entry in entries(store).items():
        board = entry.get("board") or {}
        rows.append({"key": key, "name": entry.get("name", key), "status": entry.get("status"),
                     "type": board.get("type", key.split("/")[0]), "added": entry.get("added"),
                     "how": entry.get("how"), "matched": entry.get("matched"),
                     "caught": sent_sources.get(label(board), 0) if board else 0,
                     "set_at": entry.get("set_at")})
    rows.sort(key=lambda r: (r["status"] != "active", r["added"] or ""), reverse=False)
    suggestions = [
        {"key": key, "name": entry["name"], "matched": entry["matched"], "jobs": entry["jobs"]}
        for key, entry in discovery.ranked(store, config, discovery.MIN_MATCHED)[:limit]
    ]
    return {"auto": rows, "suggestions": suggestions,
            "configured": len(config.get("sources") or []) - sum(
                r["status"] == "active" for r in rows),
            "min_matched": MIN_MATCHED, "per_week": ADDS_PER_WEEK}
