"""Dashboard actions -> vote records, through a GitHub issue inbox.

Actions are 👍 / 👎 / ✅ and the application board's stage, per-stage dates
and notes. One comment carries every change from one click or edit.

The dashboard is a static page, so a click can't write state directly. It
posts the action as an encrypted comment on one locked issue in this repo,
and the comment's ``issue_comment`` event starts a run that applies it.

Why an issue rather than the obvious alternatives:

- Not the dispatch payload (``repository_dispatch`` / ``workflow_dispatch``
  inputs). Polls share a concurrency group, and GitHub cancels a *pending*
  run when a newer one queues - so a vote riding in a run's payload is lost
  whenever a cron tick lands behind it. A comment outlives any run: whichever
  run comes next reads it.
- Not a file committed to the repo: that token would need Contents write,
  and a leaked Contents token can push code that runs with the repo's
  secrets. The page's token needs only Issues write, so the worst a leaked
  one can do is post comments.

Comments are sealed with the private-layer key (scraper.private) because the
repo is public. AES-GCM authenticates them too: a comment that doesn't open
was not written by someone holding the passphrase, and is ignored (left in
place, logged - it could be a vote sealed under an old passphrase).

Never-miss, the same way as the Telegram offset: a run applies new comments
and saves their ids in ``feedback.inbox.applied``, in the state file it is
about to commit. Only a later run, which loaded that committed state, deletes
them. If the commit fails, the next run starts from the older state and
applies them again, which is safe: actions *set* a field rather than toggle
it, and each carries the time it was clicked, so a replayed or late action
never overrides a newer change from either the page or Telegram.

GitHub REST endpoints used (``GITHUB_TOKEN`` from Actions, with
``issues: write``): list/create issues and labels, lock the issue, list and
delete issue comments.
"""

import json
import logging
import os
import re
from datetime import UTC, datetime

import requests

from scraper import feedback, private
from scraper import notify as telegram
from scraper.models import Job
from scraper.store import SeenStore

log = logging.getLogger(__name__)

API = "https://api.github.com"
LABEL = "hunter-inbox"
TITLE = "Hunter inbox"
BODY = (
    "Votes and applications from the Hunter dashboard arrive here as encrypted comments. "
    "Each run applies them and removes them. Locked, so only the repo owner can post."
)
TIMEOUT = 20
NOTES_MAX = 4000
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _valid(field: str, value) -> bool:
    """Whether a dashboard may set ``field`` to ``value``. "date:<stage>" is
    the day the job entered that board stage."""
    if field == "vote":
        return value in ("up", "down", None)
    if field == "applied":
        return isinstance(value, bool)
    if field == "stage":
        return value is None or value in feedback.STAGES
    if field == "notes":
        return value is None or (isinstance(value, str) and len(value) <= NOTES_MAX)
    if field.startswith("date:") and field[5:] in feedback.STAGES:
        return value is None or (isinstance(value, str) and bool(_DATE.fullmatch(value)))
    return False


def repo() -> str | None:
    return os.environ.get("GITHUB_REPOSITORY") or None


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or None


def process(store: SeenStore, jobs: list[Job], now: datetime | None = None) -> int:
    """Apply every new dashboard action; return how many were applied. Off
    (no API calls at all) without a passphrase, a token or a repo name."""
    phrase = private.passphrase()
    if phrase is None or _token() is None or repo() is None:
        return 0
    state = store.feedback.setdefault("inbox", {})
    issue = _issue(state)
    by_id = {job.id: job for job in jobs}
    done = set(state.get("applied", []))
    kept, applied = [], []
    for comment in _comments(issue, state):
        if comment["id"] in done:
            # Applied by an earlier run whose state was committed - we loaded it.
            if not _delete(comment["id"]):
                kept.append(comment["id"])
            continue
        actions = _open(comment, phrase)
        if actions is None:
            continue
        for action in actions:
            apply(store, action, by_id, now or datetime.now(UTC))
        applied.append(comment["id"])
    state["applied"] = kept + applied
    return len(applied)


def apply(store: SeenStore, action: dict, jobs: dict[str, Job], now: datetime) -> bool:
    """Set one field on one job's vote record, unless that field changed
    more recently. Returns whether anything changed."""
    job_id, field, value = action["id"], action["field"], action["value"]
    clicked = _parse(action["at"])
    token = telegram.callback_token(job_id)
    votes = store.feedback.setdefault("votes", {})
    record = dict(votes.get(token) or _snapshot(store, token, job_id, jobs, action))
    record.pop("sent_at", None)
    stamps = record.setdefault("set_at", {})
    if field in stamps and _parse(stamps[field]) > clicked:
        log.info("Dashboard: %s on %s is older than the last change; ignored.", field, job_id)
        return False

    stamp = clicked.isoformat(timespec="milliseconds")
    record.setdefault("vote", None)
    record.setdefault("applied", False)
    if field.startswith("date:"):
        dates = record.setdefault("stage_dates", {})
        if value:
            dates[field[5:]] = value
        else:
            dates.pop(field[5:], None)
        if not dates:
            del record["stage_dates"]
    elif field in ("stage", "notes") and not value:
        record.pop(field, None)  # off the board / notes cleared
    else:
        record[field] = value
    if field == "applied":
        if value:
            record["applied_at"] = stamp
        else:
            record.pop("applied_at", None)
    stamps[field] = stamp
    record["updated_at"] = max(record.get("updated_at", ""), now.isoformat(timespec="seconds"))
    feedback.save(votes, token, record)
    log.info("Dashboard: %s=%s on %s", field, value, job_id)
    return True


def _snapshot(store: SeenStore, token: str, job_id: str, jobs: dict[str, Job],
              action: dict) -> dict:
    """What we know about a job never voted on: its "sent" snapshot (no age
    limit here, unlike Telegram buttons), else this run's fetch, else the
    row the page showed - a job from the "Not sent" tab that has since
    closed."""
    if snapshot := store.feedback.get("sent", {}).get(token):
        return snapshot
    if job := jobs.get(job_id):
        return feedback.snapshot(job)
    row = action.get("row") or {}
    return {"id": job_id, "title": row.get("t", ""), "company": row.get("c", ""),
            "location": row.get("l", ""), "url": row.get("u", ""),
            "posted_at": row.get("d") or None,
            "source": store.insights.get("sources", {}).get(job_id.rsplit(":", 1)[0], ""),
            "category": row.get("k", "")}


def _open(comment: dict, phrase: str) -> list[dict] | None:
    """The actions in a comment, or None if it isn't one we can trust - all
    of them, since one comment is one click or edit. A comment holds either
    {"actions": [...]} or, from older pages, a single action."""
    try:
        payload = private.unseal(json.loads(comment["body"]), phrase)
        actions = payload.get("actions", [payload])
        for action in actions:
            if (not isinstance(action.get("id"), str) or not isinstance(action.get("field"), str)
                    or not _valid(action["field"], action.get("value"))):
                raise ValueError(f"unexpected action {action!r}")
            _parse(action["at"])
        return actions
    except Exception as exc:  # anything unopenable is skipped, never fatal
        log.warning("Inbox comment %s is not a dashboard action we can open (%s); left in place.",
                    comment.get("id"), type(exc).__name__)
        return None


def _parse(value: str) -> datetime:
    when = datetime.fromisoformat(value)  # 3.11+ accepts JavaScript's trailing "Z"
    return when if when.tzinfo else when.replace(tzinfo=UTC)


# --- GitHub ------------------------------------------------------------------


def _issue(state: dict) -> int:
    """The inbox issue's number: remembered, found by label, or created."""
    if number := state.get("issue"):
        return number
    found = _call("GET", f"/repos/{repo()}/issues",
                  params={"labels": LABEL, "state": "all", "per_page": 1}).json()
    if found:
        number = found[0]["number"]
    else:
        _call("POST", f"/repos/{repo()}/labels", ok=(422,),  # 422: exists already
              json={"name": LABEL, "color": "5319e7"})
        number = _call("POST", f"/repos/{repo()}/issues",
                       json={"title": TITLE, "body": BODY, "labels": [LABEL]}).json()["number"]
        _call("PUT", f"/repos/{repo()}/issues/{number}/lock", json={"lock_reason": "resolved"})
        log.info("Created the dashboard inbox: issue #%d.", number)
    state["issue"] = number
    return number


def _comments(issue: int, state: dict) -> list[dict]:
    comments, page = [], 1
    while True:
        response = _call("GET", f"/repos/{repo()}/issues/{issue}/comments", ok=(404,),
                         params={"per_page": 100, "page": page})
        if response.status_code == 404:  # the issue was deleted: make a new one next run
            state.pop("issue", None)
            return []
        batch = response.json()
        comments += batch
        if len(batch) < 100:
            return comments
        page += 1


def _delete(comment_id: int) -> bool:
    try:
        _call("DELETE", f"/repos/{repo()}/issues/comments/{comment_id}", ok=(404,))
        return True
    except requests.RequestException as exc:
        log.info("Couldn't delete inbox comment %s (%s); retrying next run.", comment_id, exc)
        return False


def _call(method: str, path: str, ok: tuple[int, ...] = (), **kwargs) -> requests.Response:
    response = requests.request(method, API + path, timeout=TIMEOUT, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, **kwargs)
    if response.status_code not in ok:
        response.raise_for_status()
    return response
