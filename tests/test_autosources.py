"""Auto-added sources: boards Hunter starts polling once feeds keep delivering them late."""

import json
from datetime import UTC, datetime, timedelta

import responses
import yaml

from scraper import autosources, discovery, inbox, insights, main
from scraper.adapters import REGISTRY
from scraper.models import Job
from scraper.store import SeenStore
from tests.test_inbox import COMMENTS, batch, configured, sent_store  # noqa: F401 (fixture)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def candidate(name: str, matched: int, board: dict) -> dict:
    return {"name": name, "jobs": matched + 1, "matched": matched,
            "boards": {json.dumps(board, sort_keys=True): matched}}


def tallied(tmp_path) -> SeenStore:
    store = SeenStore(str(tmp_path / "seen.json"))
    store.insights["discovery"] = {"since": NOW.isoformat(), "gaps": {}, "candidates": {
        "workday/globalhr": candidate("RTX", 55, {"type": "workday", "company": "rtx",
                                                  "tenant": "globalhr", "host": "wd5",
                                                  "site": "rec_rtx_ext_gateway"}),
        "workday/marvell": candidate("Marvell", 18, {"type": "workday", "company": "marvell",
                                                     "tenant": "marvell", "host": "wd1",
                                                     "site": "MarvellCareers"}),
        "greenhouse/astranis": candidate("Astranis", 8, {"type": "greenhouse",
                                                         "company": "astranis"}),
        "ashby/acme": candidate("Acme", 5, {"type": "ashby", "company": "acme"}),
        "lever/tiny": candidate("Tiny", 2, {"type": "lever", "company": "tiny"}),
        "greenhouse/@www.pinterestcareers.com": candidate(
            "Pinterest", 12,
            {"type": "greenhouse", "gh_jid": "81", "via": "www.pinterestcareers.com"}),
    }}
    return store


def merged(store, base=None):
    return autosources.merged_config(base or {"sources": []}, store)


def run_update(store, now=NOW, probe=lambda board: 40, resolve=lambda jid: "pinterest"):
    autosources.update(store, merged(store), now, probe=probe, resolve=resolve)


def statuses(store):
    return {k: e["status"] for k, e in autosources.entries(store).items()}


def test_adds_the_busiest_verified_boards_capped_per_week(tmp_path):
    store = tallied(tmp_path)
    run_update(store)

    assert statuses(store) == {"workday/globalhr": "active", "workday/marvell": "active",
                               "greenhouse/pinterest": "active"}  # resolved embedded board
    rtx = autosources.entries(store)["workday/globalhr"]
    assert rtx["how"] == "auto" and rtx["matched"] == 55
    assert [s["company"] for s in merged(store)["sources"]] == ["rtx", "marvell", "pinterest"]
    # Telegram is told about each, once.
    assert len(autosources.announcements(store)) == 3
    assert "Now polling RTX directly" in autosources.announcements(store)[0][1]

    run_update(store, now=NOW + timedelta(hours=2))  # checked at most daily
    run_update(store, now=NOW + timedelta(days=2))   # weekly cap already used
    assert len(statuses(store)) == 3

    run_update(store, now=NOW + timedelta(days=8))
    assert statuses(store)["greenhouse/astranis"] == "active"
    assert "ashby/acme" in statuses(store) and "lever/tiny" not in statuses(store)  # 2 < 3


def test_boards_that_do_not_verify_are_never_added(tmp_path):
    store = tallied(tmp_path)
    run_update(store, probe=lambda board: None if board.get("company") == "rtx" else 0)
    assert statuses(store) == {}


def test_a_failing_board_is_paused_and_resume_starts_it_fresh(tmp_path):
    store = tallied(tmp_path)
    run_update(store)
    for key in list(autosources.entries(store)):
        autosources.announced(store, key)
    store.health["workday/rtx"] = {"consecutive_failures": 5}

    run_update(store, now=NOW + timedelta(hours=1))

    assert statuses(store)["workday/globalhr"] == "paused"
    assert "rtx" not in [s["company"] for s in merged(store)["sources"]]  # not fetched
    assert "workday/globalhr" in merged(store)["discovery"]["ignore"]  # nor re-suggested
    assert "Paused RTX" in autosources.announcements(store)[0][1]

    assert autosources.request(store, "workday/globalhr", "resume", NOW + timedelta(hours=2))
    assert statuses(store)["workday/globalhr"] == "active"
    assert "workday/rtx" not in store.health


def test_dashboard_add_remove_and_add_back(tmp_path):
    store = tallied(tmp_path)
    store.insights["auto_sources_checked"] = NOW.isoformat()  # no automatic adds today

    autosources.request(store, "lever/tiny", "add", NOW)  # below the auto threshold
    assert statuses(store) == {"lever/tiny": "requested"}
    run_update(store, now=NOW + timedelta(minutes=5))
    tiny = autosources.entries(store)["lever/tiny"]
    assert tiny["status"] == "active" and tiny["how"] == "dashboard"

    autosources.request(store, "lever/tiny", "remove", NOW + timedelta(minutes=10))
    assert statuses(store) == {"lever/tiny": "removed"}
    config = merged(store)
    assert config["sources"] == [] and "lever/tiny" in config["discovery"]["ignore"]
    assert "lever/tiny" not in [k for k, _ in discovery.ranked(store, config, 1)]

    # An older click arriving late changes nothing.
    assert not autosources.request(store, "lever/tiny", "add", NOW + timedelta(minutes=7))
    assert autosources.request(store, "lever/tiny", "add", NOW + timedelta(minutes=20))
    assert statuses(store) == {"lever/tiny": "requested"}


def test_a_requested_board_that_fails_its_check_is_reported(tmp_path):
    store = tallied(tmp_path)
    store.insights["auto_sources_checked"] = NOW.isoformat()
    autosources.request(store, "lever/tiny", "add", NOW)
    run_update(store, now=NOW + timedelta(minutes=5), probe=lambda board: 0)
    assert statuses(store) == {"lever/tiny": "failed"}
    assert "Couldn't add Tiny" in autosources.announcements(store)[0][1]


@responses.activate
def test_sources_page_clicks_arrive_through_the_inbox(tmp_path, configured):  # noqa: F811
    store = sent_store(tmp_path)
    store.insights["discovery"] = tallied(tmp_path).insights["discovery"]
    act = {"id": "workday/marvell", "field": "source", "value": "add", "at": NOW.isoformat()}
    bad = {"id": "workday/marvell; rm -rf", "field": "source", "value": "add",
           "at": NOW.isoformat()}
    responses.get(COMMENTS, json=[batch(1, [act]), batch(2, [bad]),
                                  batch(3, [act | {"value": "delete"}])])

    assert inbox.process(store, [], NOW) == 1
    assert statuses(store) == {"workday/marvell": "requested"}


def test_a_late_aggregator_copy_of_a_sent_job_is_not_resent(tmp_path):
    store = SeenStore(str(tmp_path / "seen.json"))

    def job(n, source, location, title="Software Engineer Intern"):
        return Job(id=f"{source}:{n}", title=title, company="Pinterest", location=location,
                   url="u", posted_at=None, description="", source=source)

    direct = job(1, "greenhouse/pinterest", "Toronto, ON")
    insights.remember_roles([direct], store)
    late = job(2, "github/SimplifyJobs/Summer2027-Internships", "Toronto, Canada")
    elsewhere = job(3, "github/SimplifyJobs/Summer2027-Internships", "Seattle, WA")
    other_role = job(4, "github/SimplifyJobs/Summer2027-Internships", "Toronto, ON",
                     title="Data Science Intern")

    kept = insights.drop_late_copies([late, elsewhere, other_role], store)

    assert kept == [elsewhere, other_role]
    # The other way round - feed first, direct board later - always sends.
    insights.remember_roles([elsewhere], store)
    assert insights.drop_late_copies([job(5, "greenhouse/pinterest", "Seattle, WA")], store)


def test_an_auto_added_board_is_seeded_silently_then_alerts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    listings = {"old": [Job(id="fake:old:1", title="Software Engineer", company="old",
                            location="Toronto, ON", url="u1", posted_at=None,
                            description="", source="fake/old")],
                "newco": [Job(id=f"fake:newco:{n}", title=f"Software Engineer {n}",
                              company="newco", location="Toronto, ON", url=f"n{n}",
                              posted_at=None, description="", source="fake/newco")
                          for n in range(1, 4)]}
    monkeypatch.setitem(REGISTRY, "fake", lambda config: listings[config["company"]])
    config = tmp_path / "sources.yaml"
    config.write_text(yaml.safe_dump({"sources": [{"type": "fake", "company": "old"}]}))
    state = tmp_path / "seen.json"
    args = ["--dry-run", "--config", str(config), "--store", str(state)]
    main.main(args)  # first run: silent seed

    store = SeenStore(str(state))
    store.insights["auto_sources"] = {"fake/newco": {
        "board": {"type": "fake", "company": "newco"}, "name": "NewCo", "status": "active",
        "added": NOW.isoformat(), "how": "auto", "matched": 4}}
    store.insights["auto_sources_checked"] = datetime.now(UTC).isoformat()
    store.save()
    capsys.readouterr()

    main.main(args)
    assert "NEW:" not in capsys.readouterr().out  # its backlog is recorded silently
    assert SeenStore(str(state)).has("fake:newco:1")

    listings["newco"].append(Job(id="fake:newco:9", title="Backend Developer", company="newco",
                                 location="Toronto, ON", url="n9", posted_at=None,
                                 description="", source="fake/newco"))
    main.main(args)
    assert "NEW: Backend Developer @ newco" in capsys.readouterr().out


def test_dashboard_lists_auto_sources_and_suggestions(tmp_path):
    from scraper import dashboard
    store = tallied(tmp_path)
    run_update(store)
    store.feedback["sent"] = {"t1": {"id": "x", "source": "workday/rtx", "sent_at": NOW.isoformat(),
                                     "title": "SWE", "company": "rtx", "location": "", "url": ""}}
    config = merged(store, {"sources": [{"type": "ashby", "company": "cohere"}]})

    data = dashboard.collect(store, None, {}, NOW, config)["sources"]

    rtx = next(r for r in data["auto"] if r["key"] == "workday/globalhr")
    assert (rtx["name"], rtx["status"], rtx["caught"]) == ("RTX", "active", 1)
    assert [s["key"] for s in data["suggestions"]] == ["greenhouse/astranis", "ashby/acme",
                                                       "lever/tiny"]
    assert data["configured"] == 1  # sources.yaml boards, not counting auto ones
    assert dashboard.collect(store, None, {}, NOW)["sources"] is None  # no config, no page
