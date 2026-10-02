"""Dashboard data: what the public page shows, and what it must never show."""

import json
import re
from datetime import UTC, datetime, timedelta

from scraper import dashboard, feedback, main
from scraper.models import Job
from scraper.store import SeenStore

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # a Wednesday


def make_job(n: int, prefix: str = "workday:ngc", source: str = "workday/northrop-grumman",
             title: str = "Software Engineer", posted: str | None = "2026-09-29") -> Job:
    return Job(id=f"{prefix}:{n}", title=title, company=source.split("/")[-1],
               location="Toronto, ON", url=f"https://example.com/{n}", posted_at=posted,
               description="", source=source)


def store_with(tmp_path, seen: dict[str, datetime]) -> SeenStore:
    path = tmp_path / "seen.json"
    jobs = {job_id: {"seen_at": when.isoformat()} for job_id, when in seen.items()}
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return SeenStore(str(path))


def inserted(page) -> str:
    """What a build put into the page: the two data slots. Everything else
    must be the template verbatim, so a secret can only hide in these."""
    text = page.read_text(encoding="utf-8")
    slots = re.findall(r'(<script id="(?:data|private)" type="application/json">)(.*?)(</script>)',
                       text, re.S)
    rest = text
    for open_tag, body, close in slots:
        rest = rest.replace(open_tag + body + close, open_tag + close, 1)
    template = dashboard.TEMPLATE.replace("__DATA__", "").replace("__PRIVATE__", "")
    assert rest == template
    return "".join(body for _, body, _ in slots)


def embedded(page) -> dict:
    text = page.read_text(encoding="utf-8")
    raw = re.search(r'<script id="data" type="application/json">(.*?)</script>', text, re.S)
    return json.loads(raw.group(1))


def test_activity_counts_weekly_postings_and_skips_the_seeding_day(tmp_path):
    seed = NOW - timedelta(days=20)
    seen = {f"workday:ngc:seed{n}": seed for n in range(50)}  # backlog seeded on one day
    seen |= {f"workday:ngc:{n}": NOW - timedelta(days=n) for n in range(1, 9)}
    seen |= {"greenhouse:stripe:1": NOW - timedelta(days=15),
             "greenhouse:stripe:2": NOW - timedelta(days=1)}
    store = store_with(tmp_path, seen)
    dashboard.remember_sources([make_job(0)], store)

    activity = dashboard.collect(store, None, {}, NOW)["activity"]

    assert len(activity["weeks"]) == dashboard.ACTIVITY_WEEKS
    assert activity["weeks"][-1] == "2026-09-28"  # this week's Monday
    ngc = next(row for row in activity["rows"] if row["label"] == "Northrop Grumman")
    assert ngc["counts"][-1] == 2  # Mon 28 + Tue 29 of this week
    assert ngc["counts"][-2] == 6  # Sep 22-27 (Sep 21 is a Monday, 9 days back: not included)
    assert sum(ngc["counts"]) == 8  # the 50-job seed day never counts
    stripe = next(row for row in activity["rows"] if row["label"] == "Stripe")
    assert stripe["counts"][-1] == 1  # its earliest day (the 15th) is treated as its seed
    assert activity["totals"][-1] == 3
    stats = dashboard.collect(store, None, {}, NOW)["stats"]
    assert stats["new_week"] == 7  # Sep 23-29 rolling, not just this week's 3


def test_a_mid_life_backlog_reload_is_not_counted_as_hiring(tmp_path):
    # Steady 5 a day for three weeks, then state is lost and 400 reload at once.
    seen = {f"greenhouse:stripe:{d}-{n}": NOW - timedelta(days=d) for d in range(1, 22)
            for n in range(5)}
    seen |= {f"greenhouse:stripe:reload{n}": NOW - timedelta(days=10) for n in range(400)}
    store = store_with(tmp_path, seen)
    rows = dashboard.collect(store, None, {}, NOW)["activity"]["rows"]
    assert max(max(row["counts"]) for row in rows) <= 35  # no 400-job spike


def test_undatable_entries_are_left_out_of_activity(tmp_path):
    path = tmp_path / "seen.json"
    jobs = {f"ashby:openai:{n}": {"seen_at": (NOW - timedelta(days=n)).isoformat()}
            for n in range(1, 6)}
    jobs["ashby:openai:bad"] = {"seen_at": "2026-T17:01:29+00:00"}  # as found in real state
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    activity = dashboard.collect(SeenStore(str(path)), None, {}, NOW)["activity"]
    assert sum(activity["totals"]) == 4  # 5 dated, minus the first (seed) day; bad one skipped


def test_lifetimes_need_enough_samples(tmp_path):
    store = store_with(tmp_path, {})
    store.insights["lifetimes"] = {"workday/nvidia": [2, 4, 5, 6, 30], "lever/tiny": [3, 4]}
    rows = dashboard.collect(store, None, {}, NOW)["lifetimes"]
    assert rows == [{"label": "NVIDIA", "median": 5, "n": 5}]


def test_not_sent_is_open_jobs_filtered_newest_first_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "OPEN_LIMIT", 2)
    store = store_with(tmp_path, {})
    jobs = [make_job(1, posted="2026-09-20"), make_job(2, posted="2026-09-28"),
            make_job(3, title="Senior Staff Engineer", posted="2026-09-29"),
            make_job(4, posted="2026-09-25")]
    data = dashboard.collect(store, jobs, {"exclude_keywords": ["senior"]}, NOW)
    assert [row["u"] for row in data["open"]] == ["https://example.com/2", "https://example.com/4"]
    assert data["stats"]["open_matching"] == 2


def test_tabs_never_overlap_and_sent_jobs_carry_their_status(tmp_path):
    sent_open, sent_closed, sent_gone = make_job(1), make_job(2), make_job(3)
    duplicate_req = make_job(4)  # same company, title, location as sent_open: a "×2" copy
    never_sent = make_job(5, title="Firmware Engineer")
    store = store_with(tmp_path, {j.id: NOW for j in (sent_open, sent_closed, sent_gone)})
    feedback.remember_sent([sent_open, sent_closed, sent_gone], store, NOW)
    store.mark_closed(sent_closed.id, "2026-09-29")
    fetched = [sent_open, duplicate_req, never_sent]  # sent_gone: absent, but no closure recorded

    data = dashboard.collect(store, fetched, {}, NOW)

    status = {row["u"]: row["st"] for row in data["history"]}
    assert status == {sent_open.url: "open", sent_closed.url: "closed", sent_gone.url: ""}
    assert [row["u"] for row in data["open"]] == [never_sent.url]


def test_public_page_never_contains_votes_or_applications(tmp_path):
    job = make_job(1, title="Quant Developer")
    store = store_with(tmp_path, {job.id: NOW})
    feedback.remember_sent([job], store, NOW)
    store.feedback["votes"] = {"tok": {"id": job.id, "title": "SECRET-VOTED-TITLE", "vote": "up",
                                       "starred": True, "applied": True}}
    store.insights["starred"] = {job.id: {"title": "SECRET-STAR", "company": "x", "url": "u"}}

    page = dashboard.build(store, str(tmp_path / "site"), [job], {}, NOW)
    text = inserted(page)

    assert "Quant Developer" in text  # sent history is shown...
    for secret in ("SECRET-VOTED-TITLE", "SECRET-STAR", '"vote"', '"starred"', '"applied"'):
        assert secret not in text  # ...personal signals never are


def test_build_embeds_data_safely_and_records_the_time(tmp_path):
    job = make_job(1, title="Engineer </script><script>alert(1)</script>")
    store = store_with(tmp_path, {job.id: NOW})
    feedback.remember_sent([job], store, NOW)

    page = dashboard.build(store, str(tmp_path / "site"), None, {}, NOW)

    text = page.read_text(encoding="utf-8")
    assert "</script><script>alert(1)" not in text
    data = embedded(page)
    assert data["history"][0]["t"] == "Engineer </script><script>alert(1)</script>"
    assert data["open"] is None  # a state-only build has no "open now" tab
    assert "__DATA__" not in text
    assert store.insights["dashboard_built_at"] == NOW.isoformat(timespec="seconds")


def test_rebuilds_at_most_hourly(tmp_path):
    store = store_with(tmp_path, {})
    assert dashboard.is_due(store, NOW)
    store.insights["dashboard_built_at"] = NOW.isoformat()
    assert not dashboard.is_due(store, NOW + timedelta(minutes=59))
    assert dashboard.is_due(store, NOW + timedelta(minutes=60))


def test_dashboard_clicks_rebuild_right_away_instead_of_waiting_the_hour(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = store_with(tmp_path, {})
    store.insights["dashboard_built_at"] = datetime.now(UTC).isoformat()  # just built

    main.build_dashboard(store, [], {})
    assert not (tmp_path / dashboard.SITE_DIR).exists()  # not due
    main.build_dashboard(store, [], {}, now=True)
    assert (tmp_path / dashboard.SITE_DIR / "index.html").exists()


def test_a_failed_build_never_sinks_the_run(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(dashboard, "build", boom)
    main.build_dashboard(store_with(tmp_path, {}), [], {})  # logs, does not raise


def test_cli_builds_from_state_alone(tmp_path):
    store = store_with(tmp_path, {"workday:ngc:1": datetime.now(UTC)})
    store.save()
    out = tmp_path / "site"
    assert dashboard.main(["--store", store.path, "--out", str(out)]) == 0
    assert embedded(out / "index.html")["stats"]["tracked"] == 1


def test_page_offers_the_kit_without_embedding_anything_personal(tmp_path):
    store = store_with(tmp_path, {})
    text = dashboard.build(store, str(tmp_path / "site"), None, {}, NOW).read_text("utf-8")

    assert 'id="kit"' in text and 'class="kit-btn"' in text  # the kit is on the page
    assert "@anthropic-ai/sdk@0.129.0/+esm" in text  # a pinned SDK build, loaded on demand
    assert "dangerouslyAllowBrowser: true" in text
    assert '"anthropic-workspace-id": workspace' in text  # org-level keys name their workspace
    assert "SKILLS GAP (REFERENCE ONLY - NOT YOUR EXPERIENCE)" in text
    # Copy takes only the bullets and cover note: no score, no reference-only gap.
    assert "kitText.slice(start, gap > start ? gap : undefined)" in text
    # The ATS score is computed on the page from the requirement lists, must-haves double.
    assert "ATS MATCH" in text and "2 * ats.mustHit.length + ats.niceHit.length" in text
    # Match level is shown by color *and* icon + label, never color alone.
    for level, icon, label in (("strong", "✓", "Strong match"), ("fair", "⚠", "Fair match"),
                               ("weak", "✗", "Weak match")):
        assert f'["{level}", "{icon}", "{label}"]' in text
        assert f".ats-fill.{level}" in text
    # Kit answers are formatted by building DOM nodes, never by parsing model text as HTML.
    kit_js = text[text.index("function parseKit"):text.index("async function kitClient")]
    assert "innerHTML" not in kit_js and "insertAdjacentHTML" not in kit_js
    assert 'model: KIT_MODEL' in text and 'const KIT_MODEL = "claude-opus-5"' in text
    # resume and key are read from the viewer's own storage, never baked in
    assert "sk-ant-" not in text.replace('placeholder="sk-ant-..."', "")
    assert "localStorage.getItem(k)" in text


def test_kit_keeps_bullets_to_one_resume_line(tmp_path):
    store = store_with(tmp_path, {})
    text = dashboard.build(store, str(tmp_path / "site"), None, {}, NOW).read_text("utf-8")
    assert "const BULLET_MAX = 110;" in text
    assert "at most ${BULLET_MAX} characters" in text  # told to Claude...
    assert "bullet.length > BULLET_MAX" in text  # ...and checked on the page
