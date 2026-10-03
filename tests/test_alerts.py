"""Telegram alert settings: narrow what pings, never what Hunter tracks."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import responses
import yaml

from scraper import alerts, inbox, main
from scraper.adapters import REGISTRY
from scraper.models import Job
from scraper.store import SeenStore
from tests.test_inbox import COMMENTS, batch, configured, sent_store  # noqa: F401 (fixture)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def job(n: int, title: str, location: str) -> Job:
    return Job(id=f"fake:acme:{n}", title=title, company="acme", location=location,
               url=f"https://example.com/{n}", posted_at=datetime.now(UTC).date().isoformat(),
               description="", source="fake/acme")


JOBS = [
    job(1, "Software Engineering Intern", "Toronto, ON"),
    job(2, "Software Engineer, New Grad", "Toronto, ON"),
    job(3, "Software Engineering Intern", "Seattle, WA"),
    job(4, "Hardware Intern", "Vancouver, BC"),
    job(5, "Backend Developer Intern", "Remote"),
]


def with_settings(tmp_path, **settings) -> SeenStore:
    store = SeenStore(str(tmp_path / "seen.json"))
    if settings:
        store.feedback["alerts"] = settings
    return store


def titles(jobs):
    return [f"{j.title} @ {j.location}" for j in jobs]


def test_no_settings_sends_everything(tmp_path):
    assert alerts.select(JOBS, with_settings(tmp_path)) == JOBS


def test_software_internships_in_canada(tmp_path):
    store = with_settings(tmp_path, categories=["internship"], regions=["canada"],
                          title_keywords=["software", "developer"])
    assert titles(alerts.select(JOBS, store)) == [
        "Software Engineering Intern @ Toronto, ON",
        "Backend Developer Intern @ Remote",  # a bare "Remote" never rules a job out
    ]


def test_location_words_and_pause(tmp_path):
    store = with_settings(tmp_path, locations=["vancouver"])
    assert titles(alerts.select(JOBS, store)) == ["Hardware Intern @ Vancouver, BC"]
    assert alerts.select(JOBS, with_settings(tmp_path, paused=True, regions=["us"])) == []


@pytest.mark.parametrize("value", [
    {"categories": ["phd"]}, {"regions": ["mars"]}, {"title_keywords": "software"},
    {"title_keywords": [""]}, {"locations": ["x" * 41]}, {"title_keywords": ["w"] * 21},
    {"paused": "yes"}, {"colour": "blue"}, ["internship"],
])
def test_malformed_settings_are_rejected(value):
    assert not alerts.valid(value)


def test_describe_reads_like_a_sentence():
    assert alerts.describe({"categories": ["internship"], "regions": ["canada"],
                            "title_keywords": ["software"]}) == (
        "Internships · Canada · title has “software”")
    assert alerts.describe({}) == "All levels · All regions"
    assert alerts.describe({"paused": True}).startswith("Paused")


@responses.activate
def test_the_dashboard_saves_settings_through_the_inbox(tmp_path, configured):  # noqa: F811
    store = sent_store(tmp_path)
    older = {"field": "alerts", "at": NOW.isoformat(),
             "value": {"categories": ["full_time"]}}
    newer = {"field": "alerts", "at": (NOW + timedelta(minutes=1)).isoformat(),
             "value": {"categories": ["internship"], "regions": ["canada"],
                       "title_keywords": [" software "]}}
    responses.get(COMMENTS, json=[batch(1, [newer]), batch(2, [older])])  # late arrival

    assert inbox.process(store, [], NOW) == 2

    saved = store.feedback["alerts"]
    assert saved["categories"] == ["internship"]  # the older one didn't win
    assert saved["title_keywords"] == ["software"]
    assert saved["announce"] is True


@responses.activate
def test_a_bad_setting_rejects_its_comment(tmp_path, configured):  # noqa: F811
    store = sent_store(tmp_path)
    responses.get(COMMENTS, json=[batch(1, [{"field": "alerts", "at": NOW.isoformat(),
                                             "value": {"regions": ["mars"]}}])])
    assert inbox.process(store, [], NOW) == 0
    assert "alerts" not in store.feedback


def test_the_change_is_confirmed_in_telegram_once(tmp_path, monkeypatch):
    store = with_settings(tmp_path, categories=["internship"], title_keywords=["<b>x"],
                          announce=True)
    sent = []

    def down(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(main.telegram, "send_html", down)
    main.announce_alert_settings(store, dry_run=False)
    assert store.feedback["alerts"]["announce"] is True  # retried next run

    monkeypatch.setattr(main.telegram, "send_html", sent.append)
    main.announce_alert_settings(store, dry_run=False)
    main.announce_alert_settings(store, dry_run=False)
    assert len(sent) == 1
    assert "Alert settings updated" in sent[0] and "&lt;b&gt;x" in sent[0]  # escaped


def test_skipped_jobs_are_recorded_not_resent_and_stay_on_the_dashboard(tmp_path, monkeypatch,
                                                                         capsys):
    monkeypatch.chdir(tmp_path)
    listing = {"jobs": [job(0, "Software Engineer", "Toronto, ON")]}
    monkeypatch.setitem(REGISTRY, "fake", lambda config: listing["jobs"])
    config = tmp_path / "sources.yaml"
    config.write_text(yaml.safe_dump({"sources": [{"type": "fake", "company": "acme"}]}))
    state = tmp_path / "seen.json"
    args = ["--dry-run", "--config", str(config), "--store", str(state)]
    main.main(args)  # silent seed

    data = json.loads(state.read_text())
    data.setdefault("feedback", {})["alerts"] = {"categories": ["internship"],
                                                 "regions": ["canada"]}
    state.write_text(json.dumps(data))
    listing["jobs"] = [*listing["jobs"], *JOBS[:3]]
    capsys.readouterr()
    main.main(args)

    out = capsys.readouterr().out
    assert "NEW: Software Engineering Intern @ acme (Toronto, ON)" in out
    assert "New Grad" not in out and "Seattle" not in out
    store = SeenStore(str(state))
    assert all(store.has(j.id) for j in JOBS[:3])  # skipped jobs recorded: never "new" again

    main.main(args)
    assert "NEW:" not in capsys.readouterr().out
