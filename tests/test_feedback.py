"""Inline buttons: rendering, and turning presses into votes and applications."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests
import responses

from scraper import feedback, main
from scraper import notify as telegram
from scraper.models import Job
from scraper.store import SeenStore

CHAT = "4242"


@pytest.fixture(autouse=True)
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)


def make_job(n: int = 1, title: str = "Software Engineer", location: str = "Toronto, ON") -> Job:
    return Job(
        id=f"github:SimplifyJobs/New-Grad-Positions:0000-{n:04d}-aaaa-bbbb-cccccccccccc",
        title=title,
        company="Acme",
        location=location,
        url=f"https://example.com/{n}",
        posted_at="2026-09-26",
        description="",
        source="github/SimplifyJobs/New-Grad-Positions",
    )


def press(update_id: int, action: str, job: Job, markup: dict | None = None,
          chat: str = CHAT) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"q{update_id}",
            "data": f"{action}:{telegram.callback_token(job.id)}",
            "message": {
                "message_id": 7,
                "chat": {"id": int(chat)},
                "reply_markup": markup or telegram.keyboard([telegram.callback_token(job.id)]),
            },
        },
    }


@pytest.fixture
def bot(monkeypatch):
    """Fake Bot API: queue updates, record keyboard edits and answers."""
    state = {"updates": [], "edits": [], "answers": [], "offsets": []}

    def get_updates(offset):
        state["offsets"].append(offset)
        return [u for u in state["updates"] if offset is None or u["update_id"] >= offset]

    monkeypatch.setattr(telegram, "get_updates", get_updates)
    monkeypatch.setattr(telegram, "edit_keyboard",
                        lambda chat, mid, markup: state["edits"].append(markup))
    monkeypatch.setattr(telegram, "answer_callback",
                        lambda qid, text: state["answers"].append(text))
    return state


def sent_store(tmp_path, jobs: list[Job]) -> SeenStore:
    store = SeenStore(str(tmp_path / "seen.json"))
    for job in jobs:
        store.add(job)
    feedback.remember_sent(jobs, store)
    return store


# --- rendering -------------------------------------------------------------------


def test_callback_data_fits_telegrams_64_byte_limit():
    job = make_job()
    assert len(job.id) > 64  # aggregator ids alone are too long
    for button in telegram.keyboard([telegram.callback_token(job.id)])["inline_keyboard"][0]:
        assert len(button["callback_data"].encode()) <= 64


def test_single_message_row_and_state_marks():
    row = telegram.button_row("abc")
    assert [b["text"] for b in row] == ["✅ Applied"]  # 👍/👎 are on the dashboard; no ⭐
    assert [b["callback_data"] for b in row] == ["a:abc"]
    row = telegram.button_row("abc", applied=True)
    assert [b["text"] for b in row] == ["✅ Applied ✓"]


def test_digest_entries_are_numbered_to_match_their_button_rows():
    jobs = [make_job(n, title=f"Engineer, Team {n}x") for n in range(3)]
    [(text, tokens)] = telegram.digest_messages(jobs)
    assert "1. <b>Acme</b>" in text and "3. <b>Acme</b>" in text
    assert tokens == [telegram.callback_token(job.id) for job in jobs]
    rows = telegram.keyboard(tokens, numbered=True)["inline_keyboard"]
    assert [row[0]["text"] for row in rows] == ["1 ✅", "2 ✅", "3 ✅"]


def test_digest_caps_entries_per_message_and_restarts_numbering():
    jobs = [make_job(n, title=f"Engineer, Team {n}x") for n in range(20)]
    messages = telegram.digest_messages(jobs)
    assert [len(tokens) for _, tokens in messages] == [telegram.MAX_ENTRIES, 5]
    assert messages[1][0].startswith("<b>💼 FULL-TIME (20)</b> (continued)")
    assert "\n1. <b>Acme</b>" in messages[1][0]
    assert "16. " not in messages[1][0]


def test_restyle_redraws_only_the_pressed_row_and_keeps_its_number():
    tokens = ["aaa", "bbb"]
    markup = telegram.keyboard(tokens, numbered=True)
    restyled = telegram.restyle(markup, "bbb", applied=True)
    rows = restyled["inline_keyboard"]
    assert rows[0] == markup["inline_keyboard"][0]
    assert [b["text"] for b in rows[1]] == ["2 ✅ ✓"]


@responses.activate
def test_send_attaches_the_keyboard():
    responses.post("https://api.telegram.org/bott0k/sendMessage", json={"ok": True})
    job = make_job()
    telegram.send(job)
    payload = json.loads(responses.calls[0].request.body)
    [row] = payload["reply_markup"]["inline_keyboard"]
    assert row[0]["callback_data"] == f"a:{telegram.callback_token(job.id)}"


# --- presses -----------------------------------------------------------------------


def test_a_star_press_on_an_old_message_is_answered_and_changes_nothing(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])

    bot["updates"] = [press(10, "s", job)]
    assert feedback.process_updates(store) == 1
    assert store.feedback["offset"] == 11  # consumed, not retried forever
    assert store.feedback.get("votes", {}) == {}
    assert "Stars were removed" in bot["answers"][0]


def test_votes_are_exclusive_and_toggle(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])
    token = telegram.callback_token(job.id)

    bot["updates"] = [press(1, "u", job), press(2, "d", job)]
    feedback.process_updates(store)
    assert store.feedback["votes"][token]["vote"] == "down"

    bot["updates"] = [press(3, "d", job)]
    feedback.process_updates(store)
    assert token not in store.feedback["votes"]


def test_votes_keep_a_snapshot_after_the_job_leaves_sent(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])
    bot["updates"] = [press(1, "u", job)]
    feedback.process_updates(store)
    store.feedback["sent"] = {}  # pruned
    bot["updates"] = [press(2, "a", job)]
    feedback.process_updates(store)
    vote = store.feedback["votes"][telegram.callback_token(job.id)]
    assert vote["vote"] == "up" and vote["applied"] is True
    assert vote["category"] == "full_time"


def test_press_on_an_unknown_job_is_answered_and_skipped(tmp_path, bot):
    store = SeenStore(str(tmp_path / "seen.json"))
    bot["updates"] = [press(5, "u", make_job())]
    feedback.process_updates(store)
    assert store.feedback.get("votes") == {}
    assert "too old" in bot["answers"][0]
    assert store.feedback["offset"] == 6  # consumed, not retried forever


def test_press_from_another_chat_is_ignored(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])
    bot["updates"] = [press(1, "a", job, chat="999")]
    feedback.process_updates(store)
    assert store.feedback.get("votes", {}) == {}


def test_failed_cosmetic_calls_do_not_lose_the_vote(tmp_path, bot, monkeypatch):
    def too_old(*args):
        raise requests.exceptions.HTTPError("400 query is too old")

    monkeypatch.setattr(telegram, "answer_callback", too_old)
    monkeypatch.setattr(telegram, "edit_keyboard", too_old)
    job = make_job()
    store = sent_store(tmp_path, [job])
    bot["updates"] = [press(1, "u", job)]
    feedback.process_updates(store)
    assert store.feedback["votes"][telegram.callback_token(job.id)]["vote"] == "up"


def test_offset_is_sent_back_so_presses_are_confirmed_once(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])
    bot["updates"] = [press(1, "u", job)]
    feedback.process_updates(store)
    feedback.process_updates(store)
    assert bot["offsets"] == [None, 2]
    assert store.feedback["votes"][telegram.callback_token(job.id)]["vote"] == "up"


def test_state_round_trips(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])
    bot["updates"] = [press(1, "a", job)]
    feedback.process_updates(store)
    store.save()
    reloaded = SeenStore(store.path)
    assert reloaded.feedback == store.feedback


def test_sent_history_outlives_its_buttons_and_votes_outlive_both(tmp_path, bot):
    job, other = make_job(1), make_job(2)
    store = sent_store(tmp_path, [job, other])
    bot["updates"] = [press(1, "u", job)]
    feedback.process_updates(store)

    # Past SENT_DAYS: still in the dashboard history, but its buttons expired.
    later = datetime.now(UTC) + timedelta(days=feedback.SENT_DAYS + 1)
    feedback.prune(store, later)
    assert len(store.feedback["sent"]) == 2
    bot["updates"] = [press(2, "a", other)]
    feedback.process_updates(store, now=later)
    assert "too old" in bot["answers"][-1]
    assert telegram.callback_token(other.id) not in store.feedback["votes"]

    feedback.prune(store, datetime.now(UTC) + timedelta(days=feedback.HISTORY_DAYS + 1))
    assert store.feedback["sent"] == {}
    assert len(store.feedback["votes"]) == 1  # the training set is never pruned


# --- pipeline ------------------------------------------------------------------------


def test_notify_remembers_what_it_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(main.telegram, "send", lambda job, **_: None)
    monkeypatch.setattr(main.telegram, "send_digest", lambda jobs, notes=None: None)
    store = SeenStore(str(tmp_path / "seen.json"))
    main.notify([make_job(1)], store, dry_run=False, digest_threshold=10)
    many = [make_job(n, title=f"Engineer, Team {n}x") for n in range(2, 14)]
    main.notify(many, store, dry_run=False, digest_threshold=10)
    assert len(store.feedback["sent"]) == 13


def test_a_broken_update_read_never_sinks_the_run(tmp_path, monkeypatch):
    def down(offset):
        raise requests.exceptions.ConnectionError("telegram down")

    monkeypatch.setattr(telegram, "get_updates", down)
    store = SeenStore(str(tmp_path / "seen.json"))
    main.read_button_presses(store)  # logs, does not raise
    assert "offset" not in store.feedback


def test_applied_press_records_when_and_toggles_off(tmp_path, bot):
    job = make_job()
    store = sent_store(tmp_path, [job])
    token = telegram.callback_token(job.id)

    bot["updates"] = [press(1, "a", job)]
    feedback.process_updates(store)
    record = store.feedback["votes"][token]
    assert record["applied"] is True and "applied_at" in record
    assert [b["text"] for b in bot["edits"][0]["inline_keyboard"][0]][-1] == "✅ Applied ✓"
    assert bot["answers"][0] == "✅ Marked applied"

    bot["updates"] = [press(2, "a", job)]
    feedback.process_updates(store)
    assert token not in store.feedback["votes"]


def test_weekly_summary_reports_the_funnel_privately(tmp_path, bot):
    jobs = [make_job(n, title=f"Engineer, Team {n}x") for n in range(5)]
    store = sent_store(tmp_path, jobs)
    start = datetime.now(UTC) - timedelta(days=8)
    store.feedback["summary_since"] = start.isoformat()
    bot["updates"] = [press(1, "u", jobs[0]), press(2, "u", jobs[1]), press(3, "d", jobs[2]),
                      press(5, "a", jobs[0])]
    feedback.process_updates(store)

    summary = feedback.weekly_summary(store)
    assert "Sent 5 jobs" in summary
    assert "👍 2 · 👎 1" in summary
    assert "⭐" not in summary and "starred" not in summary
    assert "✅ 1 applied this week · 1 all-time" in summary

    feedback.mark_summarised(store)
    assert feedback.weekly_summary(store) is None  # not due for another week
