"""Search discovery: web search finds boards, a live check decides, autosources polls."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import responses

from scraper import autosources, main, searchdiscovery
from scraper.adapters import REGISTRY
from scraper.models import Job
from scraper.store import SeenStore

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
FILTERS = {"include_keywords": ["engineer", "intern"], "exclude_keywords": ["senior"]}


def result(url: str, title: str = "Software Engineer Intern") -> dict:
    return {"url": url, "title": title, "content": "", "score": 0.9}


def job(company: str, n: int, title: str) -> Job:
    return Job(id=f"x:{company}:{n}", title=title, company=company, location="Toronto, ON",
               url="u", posted_at=None, description="", source=f"x/{company}")


BOARDS = {  # what each board lists right now
    "acme": [job("acme", 1, "Software Engineer Intern"), job("acme", 2, "Recruiter")],
    "quiet": [job("quiet", 1, "Senior Software Engineer")],  # nothing passes the filters
    "known": [job("known", 1, "Software Engineer Intern")],
}


@pytest.fixture
def boards(monkeypatch):
    calls = []

    def fetch(config):
        calls.append(config["company"])
        return BOARDS[config["company"]]

    monkeypatch.setitem(REGISTRY, "ashby", fetch)
    monkeypatch.setitem(REGISTRY, "greenhouse", fetch)
    return calls


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(searchdiscovery.KEY_ENV, "tvly-test")


def config(**extra) -> dict:
    return {"sources": [{"type": "greenhouse", "company": "known"}], "filters": FILTERS,
            "search_discovery": {"queries": ["software intern Canada"]}} | extra


def search_returns(*urls):
    responses.post(searchdiscovery.API_URL,
                   json={"results": [result(u) for u in urls]})


@responses.activate
def test_adds_boards_with_matching_open_jobs_and_remembers_the_rest(tmp_path, keyed, boards):
    store = SeenStore(str(tmp_path / "s.json"))
    search_returns("https://jobs.ashbyhq.com/acme/1b2c", "https://jobs.ashbyhq.com/acme/9f",
                   "https://jobs.ashbyhq.com/quiet/77",
                   "https://job-boards.greenhouse.io/known/jobs/5",  # already polled
                   "https://example.com/careers/1")                  # not a job board

    assert searchdiscovery.run(store, config(), NOW) == 1

    acme = autosources.entries(store)["ashby/acme"]
    assert (acme["status"], acme["how"], acme["matched"]) == ("active", "search", 1)
    assert boards == ["acme", "quiet"]  # "known" is skipped, never fetched
    assert store.insights["search_discovery"]["checked"] == {"ashby/quiet": "2026-11-05"}
    request = json.loads(responses.calls[0].request.body)
    assert request["query"] == "software intern Canada"
    assert "jobs.ashbyhq.com" in request["include_domains"]
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tvly-test"
    [(key, text)] = autosources.announcements(store)
    assert key == "ashby/acme" and "Found by web search: 1 of its open jobs" in text


@responses.activate
def test_runs_daily_and_rechecks_a_rejected_board_only_after_two_weeks(tmp_path, keyed, boards):
    store = SeenStore(str(tmp_path / "s.json"))
    search_returns("https://jobs.ashbyhq.com/quiet/1")
    searchdiscovery.run(store, config(), NOW)

    assert searchdiscovery.run(store, config(), NOW + timedelta(hours=3)) == 0
    assert len(responses.calls) == 1  # not due: no search spent

    searchdiscovery.run(store, config(), NOW + timedelta(days=2))
    assert boards == ["quiet"]  # searched again, but not re-checked

    searchdiscovery.run(store, config(), NOW + timedelta(days=15))
    assert boards == ["quiet", "quiet"]


@responses.activate
def test_removed_boards_and_the_weekly_cap_are_respected(tmp_path, keyed, boards, monkeypatch):
    monkeypatch.setattr(searchdiscovery, "ADDS_PER_WEEK", 1)
    monkeypatch.setitem(BOARDS, "beta", [job("beta", 1, "Software Engineer Intern")])
    store = SeenStore(str(tmp_path / "s.json"))
    store.insights["auto_sources"] = {"ashby/acme": {"status": "removed", "name": "Acme"}}
    merged = autosources.merged_config(config(), store)
    search_returns("https://jobs.ashbyhq.com/acme/1", "https://jobs.ashbyhq.com/beta/1",
                   "https://jobs.ashbyhq.com/beta/2", "https://jobs.lever.co/gamma/1")

    searchdiscovery.run(store, merged, NOW)

    statuses = {k: e["status"] for k, e in autosources.entries(store).items()}
    assert statuses == {"ashby/acme": "removed", "ashby/beta": "active"}  # cap of 1 reached


def test_off_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv(searchdiscovery.KEY_ENV, raising=False)
    with responses.RequestsMock() as mock:  # any request would fail the test
        assert searchdiscovery.run(SeenStore(str(tmp_path / "s.json")), config(), NOW) == 0
        assert not mock.calls


@responses.activate
def test_a_failed_query_doesnt_stop_the_others(tmp_path, keyed, boards):
    store = SeenStore(str(tmp_path / "s.json"))
    responses.post(searchdiscovery.API_URL, status=432)  # Tavily: out of credits
    search_returns("https://jobs.ashbyhq.com/acme/1")

    searchdiscovery.run(store, config(search_discovery={"queries": ["a", "b"]}), NOW)

    assert autosources.entries(store)["ashby/acme"]["status"] == "active"


@responses.activate
def test_a_workday_result_becomes_a_pollable_board(tmp_path, keyed, monkeypatch):
    seen = []
    monkeypatch.setitem(REGISTRY, "workday",
                        lambda c: seen.append(c) or [job("x", 1, "Software Engineer Intern")])
    store = SeenStore(str(tmp_path / "s.json"))
    search_returns("https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Toronto/SWE-Intern_R1")

    searchdiscovery.run(store, config(), NOW)

    entry = autosources.entries(store)["workday/acme"]
    assert entry["board"] == {"type": "workday", "company": "acme", "tenant": "acme",
                              "host": "wd3", "site": "Careers"}
    assert autosources.label(entry["board"]) == "workday/acme"


def test_a_search_failure_never_stops_auto_sources(tmp_path, monkeypatch):
    def boom(*args):
        raise RuntimeError("tavily down")

    monkeypatch.setattr(searchdiscovery, "run", boom)
    updated = []
    monkeypatch.setattr(autosources, "update", lambda store, cfg: updated.append(True))
    main.update_auto_sources(SeenStore(str(tmp_path / "s.json")), {}, dry_run=True)
    assert updated == [True]
