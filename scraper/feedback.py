"""Button presses -> stars, votes and applications.

GitHub Actions can't host a webhook, so presses are collected by polling
getUpdates at the start of each run instead. That makes a press take effect
on the next cron tick (~5-15 minutes), and means the bot must have no
webhook set - Telegram refuses getUpdates while one exists.

Telegram keeps an update until a later getUpdates call passes a higher
offset. The offset is saved in the same state file as the votes it
produced, so a run that crashes or fails to push its state just re-reads
the same presses next run (never-miss). Presses are toggles, which is safe
under that replay: the replaying run starts from the state before the
lost one, not after it.

State (``SeenStore.feedback``):

    offset  next update id to request
    sent    {token: job snapshot + sent_at} for every job notified in the
            last HISTORY_DAYS - the dashboard's searchable history. Buttons
            stay live for SENT_DAYS; a later press answers "too old"
    votes   {token: snapshot + vote/starred/applied} - never pruned: this is
            the labelled data a relevance model will train on, and the
            application funnel. Personal: it reaches the public dashboard
            only encrypted (scraper.private), and the weekly Telegram summary.
            ``set_at`` stamps each field when it last changed, so a dashboard
            action that arrives late (scraper.inbox) can't undo a newer press
    inbox   dashboard-action bookkeeping, owned by scraper.inbox

Snapshots are kept because by the time someone votes, the posting may be
gone from its source; descriptions are left out to keep the committed
file small (most sources have none anyway).
"""

import logging
from datetime import UTC, datetime, timedelta

import requests

from scraper import insights
from scraper import notify as telegram
from scraper.models import Job
from scraper.store import SeenStore

log = logging.getLogger(__name__)

SENT_DAYS = 14
HISTORY_DAYS = 90
SUMMARY_EVERY_DAYS = 7


def remember_sent(jobs: list[Job], store: SeenStore, now: datetime | None = None) -> None:
    sent_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    sent = store.feedback.setdefault("sent", {})
    for job in jobs:
        sent[telegram.callback_token(job.id)] = snapshot(job) | {"sent_at": sent_at}


def snapshot(job: Job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "posted_at": job.posted_at,
        "source": job.source,
        "category": telegram.categorize(job),
    }


def process_updates(store: SeenStore, now: datetime | None = None) -> int:
    """Apply every pending button press; return how many updates were read."""
    feedback = store.feedback
    updates = telegram.get_updates(feedback.get("offset"))
    for update in updates:
        if query := update.get("callback_query"):
            _apply(query, store, now or datetime.now(UTC))
        # Advance after applying: a crash mid-update replays it next run.
        feedback["offset"] = update["update_id"] + 1
    return len(updates)


def _apply(query: dict, store: SeenStore, now: datetime) -> None:
    message = query.get("message") or {}
    chat = message.get("chat", {}).get("id")
    if str(chat) != telegram.chat_id():
        log.warning("Ignoring a button press from unexpected chat %s.", chat)
        return

    code, _, token = (query.get("data") or "").partition(":")
    action = telegram.ACTIONS.get(code)
    votes = store.feedback.setdefault("votes", {})
    known = votes.get(token)
    if known is None:
        snapshot = store.feedback.get("sent", {}).get(token)
        if snapshot and now - _sent_at(snapshot) <= timedelta(days=SENT_DAYS):
            known = snapshot
    if action is None or known is None:
        _best_effort(telegram.answer_callback, query["id"], "This job is too old to vote on.")
        return

    record = {key: value for key, value in known.items() if key != "sent_at"}
    record.setdefault("vote", None)
    record.setdefault("starred", False)
    record.setdefault("applied", False)
    stamp = now.isoformat(timespec="seconds")
    if action == "star":
        record["starred"] = not record["starred"]
        if record["starred"]:
            insights.star(store, _job(record))
        else:
            insights.unstar(store, record["id"])
    elif action == "applied":
        record["applied"] = not record["applied"]
        if record["applied"]:
            record["applied_at"] = stamp
        else:
            record.pop("applied_at", None)
    else:
        record["vote"] = None if record["vote"] == action else action
    record["updated_at"] = stamp
    record.setdefault("set_at", {})[_FIELDS[action]] = stamp
    save(votes, token, record)
    log.info("Button: %s on %s -> vote=%s starred=%s applied=%s",
             action, record["id"], record["vote"], record["starred"], record["applied"])

    if markup := message.get("reply_markup"):
        restyled = telegram.restyle(markup, token, record["starred"], record["applied"])
        _best_effort(telegram.edit_keyboard, chat, message["message_id"], restyled)
    _best_effort(telegram.answer_callback, query["id"], _confirmation(action, record))


_FIELDS = {"star": "starred", "applied": "applied", "up": "vote", "down": "vote"}


def save(votes: dict, token: str, record: dict) -> None:
    """A job with no vote, star or application carries no label; keep the
    training set to real signals. Its "sent" snapshot allows a later press."""
    if record.get("vote") is None and not record.get("starred") and not record.get("applied"):
        votes.pop(token, None)
    else:
        votes[token] = record


def _confirmation(action: str, record: dict) -> str:
    if action == "star":
        return "⭐ Starred - you'll hear if it closes" if record["starred"] else "Unstarred"
    if action == "applied":
        return "✅ Marked applied" if record["applied"] else "Application unmarked"
    return {"up": "👍 Noted", "down": "👎 Noted"}.get(record["vote"], "Vote cleared")


def _best_effort(call, *args) -> None:
    """Keyboard edits and callback answers are cosmetic, and routinely fail
    for presses read minutes later ("query is too old", "message is not
    modified"); the vote itself is already recorded."""
    try:
        call(*args)
    except requests.exceptions.RequestException as exc:
        log.info("Telegram cosmetic call failed (%s); vote already recorded.", exc)


def _job(record: dict) -> Job:
    return Job(
        id=record["id"],
        title=record["title"],
        company=record["company"],
        location=record["location"],
        url=record["url"],
        posted_at=record["posted_at"],
        description="",
        source=record["source"],
    )


def prune(store: SeenStore, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=HISTORY_DAYS)
    sent = store.feedback.get("sent", {})
    for token in [t for t, snap in sent.items() if _sent_at(snap) < cutoff]:
        del sent[token]


def _sent_at(snapshot: dict) -> datetime:
    try:
        return datetime.fromisoformat(snapshot["sent_at"])
    except (KeyError, TypeError, ValueError):
        return datetime.now(UTC)


# --- private weekly summary ------------------------------------------------


def weekly_summary(store: SeenStore, now: datetime | None = None) -> str | None:
    """Your application funnel for the past week, or None if not due yet.
    Sent privately to Telegram - the public dashboard never shows votes,
    stars or applications. The caller calls ``mark_summarised`` once sent."""
    now = now or datetime.now(UTC)
    feedback = store.feedback
    since = datetime.fromisoformat(
        feedback.setdefault("summary_since", now.isoformat(timespec="seconds"))
    )
    if now - since < timedelta(days=SUMMARY_EVERY_DAYS):
        return None

    sent = sum(_sent_at(snap) >= since for snap in feedback.get("sent", {}).values())
    recent = [v for v in feedback.get("votes", {}).values() if _stamp(v, "updated_at") >= since]
    up = sum(v.get("vote") == "up" for v in recent)
    down = sum(v.get("vote") == "down" for v in recent)
    starred = sum(bool(v.get("starred")) for v in recent)
    applied = sum(_stamp(v, "applied_at") >= since for v in feedback.get("votes", {}).values())
    applied_total = sum(bool(v.get("applied")) for v in feedback.get("votes", {}).values())
    closed = sum(
        1 for job_id in insights.starred(store)
        if store.has(job_id) and (on := store.closed_on(job_id)) and on >= since.date().isoformat()
    )
    lines = [
        f"📊 <b>Your week</b> (last {(now - since).days} days)",
        f"Sent {sent} jobs",
        f"👍 {up} · 👎 {down}",
        f"⭐ {starred} starred" + (f" · ⚠️ {closed} of your starred jobs closed" if closed else ""),
        f"✅ {applied} applied this week · {applied_total} all-time",
    ]
    return "\n".join(lines)


def mark_summarised(store: SeenStore, now: datetime | None = None) -> None:
    store.feedback["summary_since"] = (now or datetime.now(UTC)).isoformat(timespec="seconds")


def _stamp(record: dict, field: str) -> datetime:
    try:
        return datetime.fromisoformat(record[field])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
