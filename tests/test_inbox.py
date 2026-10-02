"""Dashboard actions: encrypted issue comments -> vote records, never-miss."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import responses

from scraper import feedback, inbox, main, private
from scraper import notify as telegram
from tests.test_dashboard import NOW, make_job, store_with

PHRASE = "correct horse battery staple"
REPO = "owner/Hunter"
COMMENTS = f"{inbox.API}/repos/{REPO}/issues/7/comments"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(private.PASSPHRASE_ENV, PHRASE)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)


def comment(cid: int, job_id: str, field: str, value, at: datetime, row: dict | None = None,
            phrase: str = PHRASE) -> dict:
    action = {"id": job_id, "field": field, "value": value,
              "at": at.isoformat().replace("+00:00", "Z")}  # as JavaScript writes it
    if row:
        action["row"] = row
    envelope = private.seal(action, phrase)
    del envelope["kdf"]  # the page sends only iv + ct
    return {"id": cid, "body": json.dumps(envelope)}


def votes(store) -> dict:
    return {r["id"]: r for r in store.feedback.get("votes", {}).values()}


def sent_store(tmp_path, *jobs):
    store = store_with(tmp_path, {job.id: NOW for job in jobs})
    feedback.remember_sent(list(jobs), store, NOW - timedelta(days=30))
    store.feedback["inbox"] = {"issue": 7}
    return store


@responses.activate
def test_applies_new_actions_and_deletes_them_only_on_the_next_run(tmp_path, configured):
    job = make_job(1)
    store = sent_store(tmp_path, job)
    responses.get(COMMENTS, json=[comment(100, job.id, "vote", "up", NOW),
                                  comment(101, job.id, "applied", True, NOW)])

    assert inbox.process(store, [], NOW) == 2
    record = votes(store)[job.id]
    assert record["vote"] == "up" and record["applied"] is True
    assert record["title"] == job.title  # from the "sent" snapshot, despite being 30 days old
    assert store.feedback["inbox"]["applied"] == [100, 101]
    assert not [c for c in responses.calls if c.request.method == "DELETE"]

    # Next run: it loaded the committed state, so those comments can go.
    responses.replace(responses.GET, COMMENTS, json=[comment(100, job.id, "vote", "up", NOW),
                                                     comment(101, job.id, "applied", True, NOW)])
    for cid in (100, 101):
        responses.delete(f"{inbox.API}/repos/{REPO}/issues/comments/{cid}", status=204)
    assert inbox.process(store, [], NOW) == 0
    assert store.feedback["inbox"]["applied"] == []


@responses.activate
def test_a_lost_state_commit_replays_harmlessly(tmp_path, configured):
    job = make_job(1)
    store = sent_store(tmp_path, job)
    batch = [comment(1, job.id, "vote", "up", NOW),
             comment(2, job.id, "vote", None, NOW + timedelta(seconds=5))]
    responses.get(COMMENTS, json=batch)

    inbox.process(store, [], NOW)
    assert job.id not in votes(store)  # up, then cleared: no label left
    store.feedback["inbox"]["applied"] = []  # as if the commit never landed
    inbox.process(store, [], NOW)
    assert job.id not in votes(store)


@responses.activate
def test_a_late_action_never_overrides_a_newer_change(tmp_path, configured):
    job = make_job(1)
    store = sent_store(tmp_path, job)
    token = telegram.callback_token(job.id)
    store.feedback["votes"] = {token: feedback.snapshot(job) | {
        "vote": "down", "starred": True, "applied": False,
        "set_at": {"vote": (NOW + timedelta(minutes=1)).isoformat()}}}
    responses.get(COMMENTS, json=[comment(1, job.id, "vote", "up", NOW),
                                  comment(2, job.id, "applied", True, NOW)])

    inbox.process(store, [], NOW)

    record = votes(store)[job.id]
    assert record["vote"] == "down"  # the newer Telegram 👎 stands
    assert record["applied"] is True  # a different field is still applied
    assert record["starred"] is True


def test_telegram_presses_stamp_the_field_they_change(tmp_path, monkeypatch):
    job = make_job(1)
    store = sent_store(tmp_path, job)
    token = telegram.callback_token(job.id)
    store.feedback["sent"][token]["sent_at"] = NOW.isoformat()
    monkeypatch.setattr(telegram, "chat_id", lambda: "42")
    monkeypatch.setattr(telegram, "answer_callback", lambda *a: None)
    query = {"id": "q", "data": f"u:{token}", "message": {"chat": {"id": 42}}}

    feedback._apply(query, store, NOW)

    assert votes(store)[job.id]["set_at"] == {"vote": NOW.isoformat(timespec="seconds")}


@responses.activate
def test_a_job_never_sent_is_snapshotted_from_the_fetch_or_the_page_row(tmp_path, configured):
    live, gone = make_job(1, title="Live Role"), make_job(2)
    store = sent_store(tmp_path)
    store.insights["sources"] = {"workday:ngc": "workday/northrop-grumman"}
    row = {"t": "Closed Role", "c": "Northrop Grumman", "l": "Toronto, ON",
           "u": "https://example.com/2", "k": "new_grad", "d": "2026-09-20"}
    responses.get(COMMENTS, json=[comment(1, live.id, "vote", "up", NOW),
                                  comment(2, gone.id, "vote", "down", NOW, row=row)])

    inbox.process(store, [live], NOW)

    assert votes(store)[live.id]["title"] == "Live Role"
    assert votes(store)[live.id]["source"] == live.source
    closed = votes(store)[gone.id]
    assert (closed["title"], closed["source"], closed["category"]) == (
        "Closed Role", "workday/northrop-grumman", "new_grad")


@responses.activate
def test_comments_that_do_not_open_are_skipped_and_kept(tmp_path, configured):
    job = make_job(1)
    store = sent_store(tmp_path, job)
    forged = comment(1, job.id, "vote", "up", NOW, phrase="someone else's passphrase")
    tampered_kdf = comment(2, job.id, "vote", "up", NOW)
    tampered_kdf["body"] = json.dumps(json.loads(tampered_kdf["body"])
                                      | {"kdf": {"iterations": 10**12, "salt": ""}})
    bad_field = comment(3, job.id, "starred", True, NOW)  # ⭐ is Telegram-only
    responses.get(COMMENTS, json=[forged, tampered_kdf, bad_field,
                                  {"id": 4, "body": "nice repo!"}])

    assert inbox.process(store, [], NOW) == 0
    assert votes(store) == {}
    assert store.feedback["inbox"]["applied"] == []


@responses.activate
def test_a_failed_delete_is_retried_next_run(tmp_path, configured):
    store = sent_store(tmp_path)
    store.feedback["inbox"]["applied"] = [5]
    responses.get(COMMENTS, json=[{"id": 5, "body": "{}"}])
    responses.delete(f"{inbox.API}/repos/{REPO}/issues/comments/5", status=502)

    inbox.process(store, [], NOW)
    assert store.feedback["inbox"]["applied"] == [5]


@responses.activate
def test_creates_a_locked_labelled_inbox_issue_once(tmp_path, configured):
    store = store_with(tmp_path, {})
    responses.get(f"{inbox.API}/repos/{REPO}/issues", json=[])
    responses.post(f"{inbox.API}/repos/{REPO}/labels", status=422)  # label already exists
    responses.post(f"{inbox.API}/repos/{REPO}/issues", json={"number": 9}, status=201)
    responses.put(f"{inbox.API}/repos/{REPO}/issues/9/lock", status=204)
    responses.get(f"{inbox.API}/repos/{REPO}/issues/9/comments", json=[])

    inbox.process(store, [], NOW)

    assert store.feedback["inbox"]["issue"] == 9
    created = json.loads(responses.calls[2].request.body)
    assert created["labels"] == [inbox.LABEL]


@responses.activate
def test_finds_an_existing_inbox_issue_and_forgets_a_deleted_one(tmp_path, configured):
    store = store_with(tmp_path, {})
    responses.get(f"{inbox.API}/repos/{REPO}/issues", json=[{"number": 7}])
    responses.get(COMMENTS, status=404)

    inbox.process(store, [], NOW)
    assert "issue" not in store.feedback["inbox"]  # found, then gone: recreate next run


@responses.activate
def test_off_without_passphrase_token_or_repo(tmp_path, monkeypatch):
    store = sent_store(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    assert inbox.process(store, [], NOW) == 0  # no passphrase
    monkeypatch.setenv(private.PASSPHRASE_ENV, PHRASE)
    monkeypatch.delenv("GITHUB_TOKEN")
    assert inbox.process(store, [], NOW) == 0  # no token (e.g. a local run)
    assert not responses.calls


def test_an_inbox_failure_never_sinks_the_run(tmp_path, monkeypatch):
    def boom(*args):
        raise RuntimeError("GitHub is down")

    monkeypatch.setattr(inbox, "process", boom)
    main.read_dashboard_actions(store_with(tmp_path, {}), [])  # logs, does not raise


def test_dashboard_publishes_the_inbox_address_and_row_ids(tmp_path, monkeypatch):
    from scraper import dashboard
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    job = make_job(1)
    store = sent_store(tmp_path, job)

    other = make_job(2, title="Data Engineer")
    data = dashboard.collect(store, [other], {}, datetime(2026, 9, 30, tzinfo=UTC))

    assert data["inbox"] == {"repo": REPO, "issue": 7}
    assert data["history"][0]["i"] == job.id
    assert data["open"][0]["i"] == other.id
