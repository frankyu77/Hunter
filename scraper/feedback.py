"""Button presses -> stars and votes.

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
    sent    {token: job snapshot} for jobs whose buttons are live; pruned
            after SENT_DAYS, after which a press answers "expired"
    votes   {token: snapshot + vote/starred} - never pruned: this is the
            labelled data a relevance model will train on

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


def remember_sent(jobs: list[Job], store: SeenStore, now: datetime | None = None) -> None:
    sent_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    sent = store.feedback.setdefault("sent", {})
    for job in jobs:
        sent[telegram.callback_token(job.id)] = _snapshot(job) | {"sent_at": sent_at}


def _snapshot(job: Job) -> dict:
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
    known = votes.get(token) or store.feedback.get("sent", {}).get(token)
    if action is None or known is None:
        _best_effort(telegram.answer_callback, query["id"], "This job is too old to vote on.")
        return

    record = {key: value for key, value in known.items() if key != "sent_at"}
    record.setdefault("vote", None)
    record.setdefault("starred", False)
    if action == "star":
        record["starred"] = not record["starred"]
        if record["starred"]:
            insights.star(store, _job(record))
        else:
            insights.unstar(store, record["id"])
    else:
        record["vote"] = None if record["vote"] == action else action
    record["updated_at"] = now.isoformat(timespec="seconds")

    # A job un-voted and un-starred carries no label; keep the training set
    # to real signals. Its "sent" snapshot still allows a later press.
    if record["vote"] is None and not record["starred"]:
        votes.pop(token, None)
    else:
        votes[token] = record
    log.info("Button: %s on %s -> vote=%s starred=%s",
             action, record["id"], record["vote"], record["starred"])

    if markup := message.get("reply_markup"):
        restyled = telegram.restyle(markup, token, record["starred"], record["vote"])
        _best_effort(telegram.edit_keyboard, chat, message["message_id"], restyled)
    _best_effort(telegram.answer_callback, query["id"], _confirmation(action, record))


def _confirmation(action: str, record: dict) -> str:
    if action == "star":
        return "⭐ Starred - you'll hear if it closes" if record["starred"] else "Unstarred"
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
    cutoff = (now or datetime.now(UTC)) - timedelta(days=SENT_DAYS)
    sent = store.feedback.get("sent", {})
    for token in [t for t, snap in sent.items() if _sent_at(snap) < cutoff]:
        del sent[token]


def _sent_at(snapshot: dict) -> datetime:
    try:
        return datetime.fromisoformat(snapshot["sent_at"])
    except (KeyError, TypeError, ValueError):
        return datetime.now(UTC)
