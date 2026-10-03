"""Telegram alert settings: which matched jobs ping your phone.

Two layers, on purpose. ``sources.yaml`` filters decide what Hunter tracks -
everything the dashboard shows. These settings, chosen on the dashboard's
"Telegram alerts" page, only narrow which of those jobs are *sent*. A job
they skip is still recorded and still on the dashboard ("Not sent", marked
New on your next visit), so a narrow alert setting never loses a job.

The setting reaches the run like a vote does: an encrypted comment on the
inbox issue (scraper.inbox), saved here in ``feedback.alerts``. So changing
it never needs a commit to main - the bot's own state commit carries it.

It is expressed as a filters config and run through scraper.filters, so
"region" and "title contains" mean exactly what they mean in sources.yaml.
No setting saved yet means everything matched is sent, as before.

State (``SeenStore.feedback["alerts"]``):

    paused          true = send no job alerts at all
    categories      levels to send: internship / new_grad / full_time
    regions         canada / us / other (a bare "Remote" always passes)
    title_keywords  title contains any of these
    locations       location contains any of these
    set_at          when it was chosen on the page (latest wins)
    announce        set when changed; cleared once Telegram confirms it
"""

import logging
from datetime import datetime

from scraper import filters
from scraper.models import Job
from scraper.store import SeenStore

log = logging.getLogger(__name__)

CATEGORIES = ("internship", "new_grad", "full_time")
REGIONS = ("canada", "us", "other")
MAX_WORDS = 20
MAX_WORD = 40
_LABELS = {"internship": "Internships", "new_grad": "New grad & junior",
           "full_time": "Full-time", "canada": "Canada", "us": "US", "other": "Other countries"}


def settings(store: SeenStore) -> dict:
    return store.feedback.get("alerts") or {}


def select(jobs: list[Job], store: SeenStore) -> list[Job]:
    """The jobs to send to Telegram, out of those that passed sources.yaml."""
    current = settings(store)
    if current.get("paused"):
        if jobs:
            log.info("Telegram alerts are paused; %d new match(es) left to the dashboard.",
                     len(jobs))
        return []
    predicates = filters.build_predicates(_as_filters(current))
    kept = [job for job in jobs if filters.keep(job, predicates)]
    if len(kept) != len(jobs):
        log.info("Alert settings skipped %d of %d new matches (still on the dashboard).",
                 len(jobs) - len(kept), len(jobs))
    return kept


def _as_filters(current: dict) -> dict:
    return {"categories": current.get("categories"), "regions": current.get("regions"),
            "include_keywords": current.get("title_keywords"),
            "locations": current.get("locations")}


def valid(value) -> bool:
    """Whether the dashboard sent a well-formed setting."""
    if not isinstance(value, dict) or set(value) - {"paused", "categories", "regions",
                                                   "title_keywords", "locations"}:
        return False
    if not isinstance(value.get("paused", False), bool):
        return False
    for key, allowed in (("categories", CATEGORIES), ("regions", REGIONS)):
        chosen = value.get(key, [])
        if not isinstance(chosen, list) or any(c not in allowed for c in chosen):
            return False
    for key in ("title_keywords", "locations"):
        words = value.get(key, [])
        if (not isinstance(words, list) or len(words) > MAX_WORDS
                or any(not isinstance(w, str) or not w.strip() or len(w) > MAX_WORD
                       for w in words)):
            return False
    return True


def apply(store: SeenStore, value: dict, clicked: datetime) -> bool:
    """Save a setting chosen on the page, unless a newer one is saved."""
    current = settings(store)
    if current.get("set_at") and datetime.fromisoformat(current["set_at"]) > clicked:
        log.info("Alert settings from the dashboard are older than the saved ones; ignored.")
        return False
    store.feedback["alerts"] = {
        "paused": bool(value.get("paused")),
        "categories": list(value.get("categories", [])),
        "regions": list(value.get("regions", [])),
        "title_keywords": [w.strip() for w in value.get("title_keywords", [])],
        "locations": [w.strip() for w in value.get("locations", [])],
        "set_at": clicked.isoformat(timespec="milliseconds"),
        "announce": True,
    }
    log.info("Alert settings updated: %s", describe(store.feedback["alerts"]))
    return True


def describe(current: dict) -> str:
    """One line for the Telegram confirmation."""
    if current.get("paused"):
        return "Paused - no job alerts until you turn them back on."
    parts = [
        " / ".join(_LABELS[c] for c in current.get("categories") or []) or "All levels",
        " / ".join(_LABELS[r] for r in current.get("regions") or []) or "All regions",
    ]
    if words := current.get("title_keywords"):
        parts.append("title has " + " or ".join(f"“{w}”" for w in words))
    if places := current.get("locations"):
        parts.append("location has " + " or ".join(f"“{p}”" for p in places))
    return " · ".join(parts)
