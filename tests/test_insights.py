"""Closures, reposts, hiring seasons, and the messages they produce."""

from datetime import UTC, datetime, timedelta

import yaml

from scraper import insights, main
from scraper.adapters import REGISTRY
from scraper.models import Job, JobNotes
from scraper.notify import (
    company_links,
    format_closed_alert,
    format_digest,
    format_message,
    format_season_alert,
)
from scraper.store import SeenStore

# store.add stamps the real clock, so "now" in these tests must be real too.
NOW = datetime.now(UTC)


def make_job(
    n: int = 1,
    title: str = "Software Engineer",
    company: str = "nvidia",
    source: str = "workday/nvidia",
    prefix: str = "workday:nvidia",
    posted_at: str | None = "2026-09-20",
) -> Job:
    return Job(
        id=f"{prefix}:{n}",
        title=title,
        company=company,
        location="Santa Clara, CA",
        url=f"https://example.com/{n}",
        posted_at=posted_at,
        description="",
        source=source,
    )


def tracked_store(tmp_path, jobs: list[Job], since_days_ago: int = 30) -> SeenStore:
    """A store already tracking closures, holding ``jobs`` as seen now."""
    store = SeenStore(str(tmp_path / "seen.json"))
    store.insights["since"] = (NOW - timedelta(days=since_days_ago)).isoformat()
    for job in jobs:
        store.add(job)
    return store


def ok(fetched: int, truncated: bool = False) -> dict:
    return {"fetched": fetched, "errors": 0, "truncated": truncated}


# --- closures ----------------------------------------------------------------


def test_tracking_starts_on_first_run_without_closing_history(tmp_path):
    store = SeenStore(str(tmp_path / "seen.json"))
    old = make_job(1)
    store.add(old)  # seen before tracking existed
    later = NOW + timedelta(hours=1)
    closures = insights.find_closures(store, [make_job(2)], {"workday/nvidia": ok(1)}, later)
    assert closures == []
    assert "since" in store.insights


def test_tracked_job_missing_from_a_clean_fetch_is_closed(tmp_path):
    jobs = [make_job(n) for n in range(5)]
    store = tracked_store(tmp_path, jobs)
    still_open = jobs[1:]
    closures = insights.find_closures(store, still_open, {"workday/nvidia": ok(4)}, NOW)
    assert closures == [(jobs[0].id, "workday/nvidia")]


def test_failed_source_closes_nothing(tmp_path):
    jobs = [make_job(n) for n in range(5)]
    store = tracked_store(tmp_path, jobs)
    stats = {"workday/nvidia": {"fetched": 4, "errors": 1}}
    assert insights.find_closures(store, jobs[1:], stats, NOW) == []


def test_capped_fetch_only_judges_jobs_inside_its_date_window(tmp_path):
    jobs = [make_job(n) for n in range(5)]
    store = tracked_store(tmp_path, jobs)
    # The capped fetch now only reaches back to postings from today, so the
    # missing job may have just scrolled out of the newest-200 window.
    window = [make_job(n, posted_at=NOW.date().isoformat()) for n in range(1, 5)]
    assert insights.find_closures(store, window, {"workday/nvidia": ok(4, True)}, NOW) == []
    # Reaching back well before the job was seen: absence now means closed.
    reach = (NOW - timedelta(days=60)).date().isoformat()
    window = [make_job(n, posted_at=reach) for n in range(1, 5)]
    closures = insights.find_closures(store, window, {"workday/nvidia": ok(4, True)}, NOW)
    assert closures == [(jobs[0].id, "workday/nvidia")]


def test_capped_fetch_without_dates_closes_nothing(tmp_path):
    jobs = [make_job(n) for n in range(5)]
    store = tracked_store(tmp_path, jobs)
    undated = [make_job(n, posted_at=None) for n in range(1, 5)]
    assert insights.find_closures(store, undated, {"workday/nvidia": ok(4, True)}, NOW) == []


def test_mass_disappearance_is_treated_as_a_partial_fetch(tmp_path):
    jobs = [make_job(n) for n in range(10)]
    store = tracked_store(tmp_path, jobs)
    assert insights.find_closures(store, jobs[:3], {"workday/nvidia": ok(3)}, NOW) == []


def test_source_that_returned_nothing_closes_nothing(tmp_path):
    jobs = [make_job(n) for n in range(3)]
    store = tracked_store(tmp_path, jobs)
    assert insights.find_closures(store, [], {"workday/nvidia": ok(0)}, NOW) == []


def test_closed_job_that_reappears_is_reopened(tmp_path):
    job = make_job(1)
    store = tracked_store(tmp_path, [job])
    store.mark_closed(job.id, "2026-09-25")
    insights.find_closures(store, [job], {"workday/nvidia": ok(1)}, NOW)
    assert store.closed_on(job.id) is None


def test_closures_feed_a_median_lifetime_once_there_are_enough(tmp_path):
    jobs = [make_job(n) for n in range(6)]
    store = tracked_store(tmp_path, jobs)
    for days, job in zip([2, 4, 5, 6, 30], jobs, strict=False):
        closed_at = store.seen_at(job.id) + timedelta(days=days)
        insights.record_closure(store, job.id, job.source, closed_at)
        assert store.closed_on(job.id)
    assert insights.typical_open_days(store, "workday/nvidia") == 5
    assert insights.typical_open_days(store, "workday/other") is None


def test_aggregator_closures_are_marked_but_not_counted(tmp_path):
    job = make_job(1, source="github/SimplifyJobs/New-Grad-Positions", prefix="github:x/y")
    store = tracked_store(tmp_path, [job])
    insights.record_closure(store, job.id, job.source, NOW)
    assert store.closed_on(job.id)
    assert "lifetimes" not in store.insights


def test_closure_state_round_trips(tmp_path):
    job = make_job(1)
    store = tracked_store(tmp_path, [job])
    insights.record_closure(store, job.id, job.source, NOW)
    store.save()
    reloaded = SeenStore(store.path)
    assert reloaded.closed_on(job.id) == NOW.date().isoformat()
    assert reloaded.insights["lifetimes"] == store.insights["lifetimes"]


# --- reposts -------------------------------------------------------------------


def test_same_role_after_the_old_posting_closed_is_a_repost(tmp_path):
    old, new = make_job(1), make_job(2)
    store = tracked_store(tmp_path, [old])
    insights.remember_roles([old], store, NOW - timedelta(days=20))
    store.mark_closed(old.id, "2026-09-20")
    notes = insights.notes_for([new], store)
    assert notes[new.id].reposted_since == (NOW - timedelta(days=20)).date().isoformat()


def test_parallel_req_while_the_old_one_is_open_is_not_a_repost(tmp_path):
    old, new = make_job(1), make_job(2)
    store = tracked_store(tmp_path, [old])
    insights.remember_roles([old], store, NOW)
    assert insights.notes_for([new], store) == {}


def test_stale_roles_are_pruned(tmp_path):
    job = make_job(1)
    store = tracked_store(tmp_path, [job])
    insights.remember_roles([job], store, NOW - timedelta(days=61))
    insights.prune(store, NOW)
    assert store.insights["roles"] == {}


# --- hiring seasons --------------------------------------------------------------


def test_cycle_prefers_title_year_then_posting_month():
    assert insights.cycle(make_job(title="Software Intern - Summer 2027")) == 2027
    assert insights.cycle(make_job(title="New Grad SWE", posted_at="2026-09-01")) == 2027
    assert insights.cycle(make_job(title="New Grad SWE", posted_at="2026-03-01")) == 2026


def test_first_new_grad_posting_opens_the_season_once_per_company(tmp_path):
    store = tracked_store(tmp_path, [])
    jobs = [make_job(n, title=f"New Grad Software Engineer {n}", posted_at="2026-09-01")
            for n in (1, 2)]
    openings = insights.season_openings(jobs, store)
    assert [(job.id, category, year) for job, category, year in openings] == [
        (jobs[0].id, "new_grad", 2027)
    ]
    insights.record_season(store, jobs[0], "new_grad", 2027)
    assert insights.season_openings(jobs, store) == []


def test_already_open_seasons_are_observed_silently(tmp_path):
    store = tracked_store(tmp_path, [])
    known = make_job(1, title="Software Engineering Intern", posted_at="2026-09-01")
    insights.observe_seasons([known], store, {})
    new = make_job(2, title="Hardware Intern", posted_at="2026-09-20")
    assert insights.season_openings([new], store) == []


def test_filtered_out_roles_do_not_preempt_the_season(tmp_path):
    store = tracked_store(tmp_path, [])
    marketing = make_job(1, title="Marketing Intern", posted_at="2026-09-01")
    insights.observe_seasons([marketing], store, {"include_keywords": ["engineer"]})
    swe = make_job(2, title="Software Engineer Intern", posted_at="2026-09-20")
    assert len(insights.season_openings([swe], store)) == 1


def test_aggregator_postings_never_open_a_season(tmp_path):
    store = tracked_store(tmp_path, [])
    job = make_job(1, title="Software Intern", source="github/SimplifyJobs/Summer2026-Internships")
    assert insights.season_openings([job], store) == []


# --- message formatting ------------------------------------------------------------


def test_company_links_cover_pay_reviews_and_referrals():
    line = company_links("jane-street", "University of Waterloo")
    assert 'href="https://www.levels.fyi/companies/jane-street/salaries"' in line
    assert "glassdoor.com/Search/results.htm?keyword=Jane+Street" in line
    assert "keywords=Jane+Street+University+of+Waterloo" in line
    assert "keywords=Jane+Street\"" in company_links("jane-street")


def test_message_shows_notes_and_links():
    notes = JobNotes(reposted_since="2026-08-01", typical_open_days=5)
    text = format_message(make_job(), notes=notes)
    assert "🔁 Reposted: first listed 2026-08-01" in text
    assert "⏳ NVIDIA postings usually stay open ~5d" in text
    assert "levels.fyi</a> · " in text


def test_digest_marks_reposts_and_typical_lifetime():
    job = make_job()
    notes = {job.id: JobNotes(reposted_since="2026-08-01", typical_open_days=5)}
    [message] = format_digest([job], notes)
    assert "Software Engineer</a> 🔁" in message
    assert "usually open ~5d" in message
    assert "levels.fyi" not in message  # links stay out of the digest


def test_season_and_closed_alerts():
    job = make_job(title="Software Intern & Co-op", company="stripe")
    alert = format_season_alert(job, "internship", 2027)
    assert alert.startswith("🚨 <b>Stripe opened Internships · Summer 2027</b>")
    assert "Software Intern &amp; Co-op" in alert
    closed = format_closed_alert({"title": "SWE", "company": "nvidia", "url": "https://x"}, 5)
    assert closed.startswith("⚠️ <b>NVIDIA</b>")
    assert "just closed" in closed and "about 5d" in closed


# --- pipeline -----------------------------------------------------------------------


def test_starred_closure_alerts_before_recording_and_retries_on_failure(tmp_path, monkeypatch):
    job = make_job(1)
    store = tracked_store(tmp_path, [job])
    insights.star(store, job)

    def down(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(main.telegram, "send_html", down)
    main.announce_closures([(job.id, job.source)], store, dry_run=False)
    assert store.closed_on(job.id) is None  # detected again next run

    sent = []
    monkeypatch.setattr(main.telegram, "send_html", sent.append)
    main.announce_closures([(job.id, job.source)], store, dry_run=False)
    assert len(sent) == 1 and "just closed" in sent[0]
    assert store.closed_on(job.id)


def test_pipeline_end_to_end_across_runs(tmp_path, monkeypatch, capsys):
    """Seed, then announce a new season, then notice a posting closing."""
    listing = {"jobs": [make_job(1, title="Staff Engineer", company="acme", source="fake/acme",
                                  prefix="fake:acme")]}
    monkeypatch.setitem(REGISTRY, "fake", lambda config: listing["jobs"])
    config = tmp_path / "sources.yaml"
    config.write_text(yaml.safe_dump({"sources": [{"type": "fake", "company": "acme"}]}))
    args = ["--dry-run", "--config", str(config), "--store", str(tmp_path / "seen.json")]

    main.main(args)  # first run ever: silent seed

    intern = make_job(2, title="Software Intern", company="acme", source="fake/acme",
                      prefix="fake:acme", posted_at=datetime.now(UTC).date().isoformat())
    listing["jobs"] = [*listing["jobs"], intern]
    main.main(args)
    out = capsys.readouterr().out
    assert f"SEASON: acme opened internship {insights.cycle(intern)}" in out
    assert "NEW: Software Intern @ acme" in out

    listing["jobs"] = listing["jobs"][:1]  # the intern posting closes
    main.main(args)
    assert SeenStore(str(tmp_path / "seen.json")).closed_on(intern.id)
