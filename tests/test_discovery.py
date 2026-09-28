"""Direct-source discovery: mining aggregator apply links for boards to poll."""

import re
from datetime import UTC, datetime, timedelta

import pytest
import responses
import yaml

from scraper import discovery, main
from scraper.models import Job
from scraper.store import SeenStore

FEED = "github/SimplifyJobs/New-Grad-Positions"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=discovery.REPORT_EVERY_DAYS)


def job(url: str, company: str = "Acme Corp", n: int = 1, source: str = FEED) -> Job:
    return Job(
        id=f"github:x:{n}", title="Software Engineer", company=company, location="Remote",
        url=url, posted_at=None, description="", source=source,
    )


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://job-boards.greenhouse.io/genevatrading/jobs/5085231007",
         {"type": "greenhouse", "company": "genevatrading"}),
        ("https://boards.greenhouse.io/spacex/jobs/8428913002?gh_jid=1",
         {"type": "greenhouse", "company": "spacex"}),
        ("https://boards.greenhouse.io/embed/job_app?token=123&for=anduril",
         {"type": "greenhouse", "company": "anduril"}),
        ("https://jobs.ashbyhq.com/onebrief/a88e10d4-66d8-4911-99e3-3d20351e73d9/application",
         {"type": "ashby", "company": "onebrief"}),
        ("https://jobs.ashbyhq.com/Acme%20Labs/abc", {"type": "ashby", "company": "Acme Labs"}),
        ("https://jobs.lever.co/theinformationlab/5c48a12b/apply",
         {"type": "lever", "company": "theinformationlab"}),
        ("https://amat.wd1.myworkdayjobs.com/External/job/Santa-ClaraCA/AI-Research_R2611980-1",
         {"type": "workday", "company": "acme-corp", "tenant": "amat", "host": "wd1",
          "site": "External"}),
        ("https://tsc.wd12.myworkdayjobs.com/en-US/TSC-Careers/job/Silver-Spring-MD/SWE_JR2567",
         {"type": "workday", "company": "acme-corp", "tenant": "tsc", "host": "wd12",
          "site": "TSC-Careers"}),
        ("https://intel.wd1.myworkdayjobs.com/en-us/External/job/Folsom/Grad_JR0271",
         {"type": "workday", "company": "acme-corp", "tenant": "intel", "host": "wd1",
          "site": "External"}),
        ("https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26007181",
         {"type": "oracle", "company": "acme-corp", "host": "egug.fa.us2.oraclecloud.com",
          "site_number": "CX_1", "site_name": "CX_1"}),
        ("https://iawmqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/careers/job/1",
         {"type": "oracle", "company": "acme-corp", "host": "iawmqy.fa.ocs.oraclecloud.com",
          "site_number": "CX_1", "site_name": "careers"}),
        ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/2107",
         {"type": "oracle", "company": "acme-corp", "host": "jpmc.fa.oraclecloud.com",
          "site_number": "CX_1001", "site_name": "CX_1001"}),
        ("https://job-boards.eu.greenhouse.io/agency/jobs/4738026101",
         {"type": "greenhouse", "company": "agency"}),
        ("https://jobs.eu.lever.co/cirrus/1979da07/apply",
         {"type": "lever", "company": "cirrus", "region": "eu"}),
        ("https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/CA/Engineer-I",
         {"type": "workday", "company": "acme-corp", "tenant": "microchiphr", "host": "wd5",
          "site": "External", "domain": "myworkdaysite.com"}),
        ("https://jobs.smartrecruiters.com/BoschGroup/744000146524429",
         {"type": "smartrecruiters", "company": "BoschGroup"}),
        ("https://apply.workable.com/tickpick/j/5840ECEB50/apply",
         {"type": "workable", "company": "tickpick"}),
        ("https://ats.rippling.com/flexai/jobs/93ada67c",
         {"type": "rippling", "company": "flexai"}),
        ("https://lexical.bamboohr.com/careers/73/", {"type": "bamboohr", "company": "lexical"}),
        ("https://qualcomm.eightfold.ai/careers/job/446717859953",
         {"type": "eightfold", "company": "acme-corp", "tenant": "qualcomm",
          "domain": "qualcomm.com"}),
        ("https://careers.amd.com/jobs/88877?icims=1",
         {"type": "jibe", "company": "acme-corp", "host": "careers.amd.com"}),
        ("https://jobs.l3harris.com/job/Greenville-TX-75402/1420469200/?ats=successfactors",
         {"type": "successfactors", "company": "acme-corp", "host": "jobs.l3harris.com"}),
        ("https://lifeattiktok.com/search/7521987177107589384",
         {"type": "tiktok", "recruitment_ids": ["2"]}),
        ("https://amazon.jobs/en/jobs/10408763/software-development-engineer-2026",
         {"type": "amazon"}),
        ("https://www.optiver.com/join-us/jobs/8451763002/?gh_jid=8451763002",
         {"type": "greenhouse", "gh_jid": "8451763002", "via": "www.optiver.com"}),
    ],
)
def test_board_for_recognises_supported_ats_links(url, board):
    assert discovery.board_for(job(url)) == board


@pytest.mark.parametrize(
    "url",
    [
        "https://careers-markon.icims.com/jobs/8107/job?mobile=true",  # native iCIMS: HTML only
        "https://lamons.applytojob.com/apply/6SA4nACfmW/Analyst",  # JazzHR: no API
        "https://jobs.apple.com/en-us/details/200657915",
        "https://www.tesla.com/careers/search/job/272816",  # blocks automated requests
        "",
    ],
)
def test_board_for_ignores_unsupported_links(url):
    assert discovery.board_for(job(url)) is None


def test_board_keys_match_config_entries_regardless_of_label_and_site():
    configured = {"type": "workday", "company": "hpe", "tenant": "hpe", "host": "wd5",
                  "site": "Jobsathpe"}
    found = discovery.board_for(job("https://hpe.wd5.myworkdayjobs.com/acjobsite/job/x"))
    assert discovery.board_key(found) == discovery.board_key(configured) == "workday/hpe"
    assert discovery.board_key({"type": "greenhouse", "company": "SpaceX"}) == "greenhouse/spacex"
    assert discovery.board_key({"type": "tiktok"}) == "tiktok"
    assert discovery.board_key({"type": "jibe", "company": "amd", "host": "careers.amd.com"}) == (
        "jibe/careers.amd.com"
    )


# --- tallying -------------------------------------------------------------------


def config(sources=(), ignore=()) -> dict:
    return {"sources": list(sources), "discovery": {"ignore": list(ignore)}}


def tally(tmp_path, jobs, matched, cfg=None) -> SeenStore:
    store = SeenStore(str(tmp_path / "seen.json"))
    discovery.observe(jobs, matched, store, cfg or config(), NOW)
    return store


def test_observe_counts_new_and_matching_aggregator_jobs(tmp_path):
    jobs = [job("https://boards.greenhouse.io/spacex/jobs/1", n=n) for n in range(3)]
    store = tally(tmp_path, jobs, matched=jobs[:2])
    entry = store.insights["discovery"]["candidates"]["greenhouse/spacex"]
    assert (entry["jobs"], entry["matched"]) == (3, 2)


def test_observe_skips_direct_sources_configured_and_ignored_boards(tmp_path):
    jobs = [
        job("https://boards.greenhouse.io/spacex/jobs/1", n=1, source="greenhouse/spacex"),
        job("https://boards.greenhouse.io/stripe/jobs/1", n=2),
        job("https://jobs.lever.co/palantir/abc", n=3),
    ]
    cfg = config(sources=[{"type": "greenhouse", "company": "stripe"}],
                 ignore=["lever/palantir"])
    store = tally(tmp_path, jobs, jobs, cfg)
    assert store.insights["discovery"]["candidates"] == {}


# --- weekly report -----------------------------------------------------------------


def spacex_and_rtx(tmp_path) -> SeenStore:
    spacex = [job("https://boards.greenhouse.io/spacex/jobs/1", "SpaceX", n) for n in range(3)]
    rtx = [job("https://globalhr.wd5.myworkdayjobs.com/rec_rtx_ext_gateway/job/x", "RTX", n)
           for n in range(10, 15)]
    rtx.append(job("https://globalhr.wd5.myworkdayjobs.com/Other_Site/job/y", "RTX", 20))
    one_off = [job("https://jobs.ashbyhq.com/tiny/abc", "Tiny", 30)]
    everything = spacex + rtx + one_off
    return tally(tmp_path, everything, matched=everything)


def test_report_waits_a_week(tmp_path):
    store = spacex_and_rtx(tmp_path)
    assert discovery.due_report(store, config(), NOW + timedelta(days=6), probe=lambda b: 5) is None


def test_report_ranks_verifies_and_renders_pasteable_yaml(tmp_path):
    store = spacex_and_rtx(tmp_path)
    report = discovery.due_report(store, config(), LATER, probe=lambda b: 12)

    assert report.startswith("🧭 <b>Worth polling directly?</b>")
    assert report.index("1. RTX") < report.index("2. SpaceX")  # 6 matching beats 3
    assert "Tiny" not in report  # a single job is below MIN_MATCHED
    assert "6 matching of 6 new jobs · 12 open now" in report

    blocks = re.findall(r"<pre>(.*?)</pre>", report, re.S)
    entries = [yaml.safe_load(block)[0] for block in blocks]
    assert entries[0] == {"type": "workday", "company": "rtx", "tenant": "globalhr",
                          "host": "wd5", "site": "rec_rtx_ext_gateway"}  # most common site
    assert entries[1] == {"type": "greenhouse", "company": "spacex"}
    assert "ignore key: <code>workday/globalhr</code>" in report


def test_report_drops_boards_that_fail_to_verify(tmp_path):
    store = spacex_and_rtx(tmp_path)
    report = discovery.due_report(
        store, config(), LATER, probe=lambda b: None if b["type"] == "workday" else -200
    )
    assert "RTX" not in report
    assert "200+ open now" in report  # capped adapter: at least this many


def test_report_rechecks_config_added_since_tallying(tmp_path):
    store = spacex_and_rtx(tmp_path)
    cfg = config(sources=[{"type": "greenhouse", "company": "spacex"}], ignore=["workday/globalhr"])
    assert discovery.due_report(store, cfg, LATER, probe=lambda b: 5) is None


def test_embedded_greenhouse_board_is_resolved_from_its_job_id(tmp_path):
    jobs = [job(f"https://www.optiver.com/join-us/jobs/{n}/?gh_jid={n}", "Optiver", n)
            for n in (101, 102, 103)]
    store = tally(tmp_path, jobs, jobs)
    entry = store.insights["discovery"]["candidates"]["greenhouse/@www.optiver.com"]
    assert entry["jobs"] == 3 and len(entry["boards"]) == 1  # one shape, not one per job

    asked = []
    report = discovery.due_report(
        store, config(), LATER, probe=lambda b: 40,
        resolve=lambda jid: asked.append(jid) or "optiverus",
    )
    assert asked == ["101"]
    assert yaml.safe_load(re.search(r"<pre>(.*?)</pre>", report, re.S).group(1)) == [
        {"type": "greenhouse", "company": "optiverus"}
    ]
    assert "ignore key: <code>greenhouse/optiverus</code>" in report


def test_embedded_greenhouse_board_already_polled_is_not_suggested(tmp_path):
    jobs = [job(f"https://www.janestreet.com/apply/{n}?gh_jid={n}", "Jane Street", n)
            for n in (1, 2)]
    store = tally(tmp_path, jobs, jobs)
    cfg = config(sources=[{"type": "greenhouse", "company": "janestreet"}])
    report = discovery.due_report(store, cfg, LATER, probe=lambda b: 5,
                                  resolve=lambda jid: "janestreet")
    assert report is None


@responses.activate
def test_resolve_greenhouse_board_reads_the_embed_redirect():
    responses.get(
        "https://boards.greenhouse.io/embed/job_app",
        status=301,
        headers={"Location": "https://job-boards.greenhouse.io/embed/job_app?for=optiverus&token=8"},
    )
    assert discovery.resolve_greenhouse_board("8") == "optiverus"
    responses.replace(responses.GET, "https://boards.greenhouse.io/embed/job_app", status=302,
                      headers={"Location": "https://job-boards.greenhouse.io/embed/job_board?for="})
    assert discovery.resolve_greenhouse_board("9") is None


def test_country_filtering_adapters_are_narrowed_to_your_regions(tmp_path):
    jobs = [job(f"https://jobs.smartrecruiters.com/BoschGroup/{n}", "Bosch", n) for n in (1, 2)]
    store = tally(tmp_path, jobs, jobs)
    cfg = config() | {"filters": {"regions": ["canada", "us"]}}
    report = discovery.due_report(store, cfg, LATER, probe=lambda b: 50)
    [entry] = yaml.safe_load(re.search(r"<pre>(.*?)</pre>", report, re.S).group(1))
    assert entry == {"type": "smartrecruiters", "company": "BoschGroup", "countries": ["ca", "us"]}


def test_unsupported_platforms_are_reported_as_gaps(tmp_path):
    icims = [job(f"https://careers-co{n}.icims.com/jobs/{n}/job", f"Co {n}", n) for n in range(5)]
    tesla = [job(f"https://www.tesla.com/careers/search/job/{n}", "Tesla", 100 + n)
             for n in range(3)]
    store = tally(tmp_path, icims + tesla, matched=icims[:4] + tesla)
    gaps = store.insights["discovery"]["gaps"]
    assert gaps["iCIMS portals"]["jobs"] == 5 and gaps["iCIMS portals"]["matched"] == 4
    assert gaps["tesla.com"]["companies"] == ["Tesla"]

    report = discovery.due_report(store, config(), LATER, probe=lambda b: 5)
    assert "<b>No adapter yet</b>" in report
    assert "• iCIMS portals — 4 matching of 5 jobs (Co 0, Co 1, Co 2 +2 more)" in report
    assert report.index("iCIMS portals") < report.index("tesla.com")
    assert "Worth polling directly?" in report and "<pre>" not in report  # gaps alone


def test_report_suggests_at_most_five(tmp_path):
    jobs = [job(f"https://boards.greenhouse.io/co{c}/jobs/{n}", f"Co {c}", c * 10 + n)
            for c in range(8) for n in range(2)]
    store = tally(tmp_path, jobs, jobs)
    report = discovery.due_report(store, config(), LATER, probe=lambda b: 3)
    assert report.count("<pre>") == discovery.SUGGESTIONS


# --- pipeline ------------------------------------------------------------------------


def test_announce_sends_then_starts_a_new_week(tmp_path, monkeypatch):
    store = spacex_and_rtx(tmp_path)
    store.insights["discovery"]["since"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    monkeypatch.setattr(discovery, "_probe", lambda board: 9)
    sent = []
    monkeypatch.setattr(main.telegram, "send_html", sent.append)

    main.announce_discovery(store, config(), dry_run=False)

    assert len(sent) == 1 and "Worth polling directly" in sent[0]
    assert store.insights["discovery"]["candidates"] == {}


def test_failed_send_keeps_the_tally_for_next_run(tmp_path, monkeypatch):
    store = spacex_and_rtx(tmp_path)
    store.insights["discovery"]["since"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    monkeypatch.setattr(discovery, "_probe", lambda board: 9)

    def down(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(main.telegram, "send_html", down)
    main.announce_discovery(store, config(), dry_run=False)
    assert store.insights["discovery"]["candidates"] != {}


def test_a_week_with_nothing_worth_suggesting_just_resets(tmp_path, monkeypatch):
    store = tally(tmp_path, [job("https://jobs.ashbyhq.com/tiny/abc")], [])
    store.insights["discovery"]["since"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    monkeypatch.setattr(main.telegram, "send_html", lambda text: pytest.fail("sent"))
    main.announce_discovery(store, config(), dry_run=False)
    assert store.insights["discovery"]["candidates"] == {}
