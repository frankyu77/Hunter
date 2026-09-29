"""Adapter mapping tests. All HTTP is mocked from recorded fixtures - no
network access. Each test asserts the fixture's real payload maps to the
canonical Job shape, including the skip rules."""

import json
import re
from datetime import UTC, datetime, timedelta

import responses

from scraper.adapters import (
    REGISTRY,
    amazon,
    ashby,
    bamboohr,
    eightfold,
    get_adapter,
    github_repo,
    greenhouse,
    jibe,
    lever,
    newest_first,
    oracle,
    rippling,
    smartrecruiters,
    successfactors,
    tiktok,
    workable,
    workday,
)


def test_registry_dispatches_all_types():
    for type_str in REGISTRY:
        assert callable(get_adapter(type_str))


def test_registry_rejects_unknown_type():
    try:
        get_adapter("taleo")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert "taleo" in str(exc)
        assert "ashby" in str(exc)  # error names the known types


def test_registry_has_no_stale_entries():
    assert set(REGISTRY) == {
        "ashby",
        "greenhouse",
        "lever",
        "github",
        "workday",
        "oracle",
        "smartrecruiters",
        "workable",
        "rippling",
        "bamboohr",
        "jibe",
        "successfactors",
        "eightfold",
        "tiktok",
        "amazon",
    }


@responses.activate
def test_ashby_maps_jobs_and_skips_unlisted(fixture):
    responses.get(
        "https://api.ashbyhq.com/posting-api/job-board/wealthsimple",
        json=fixture("ashby_wealthsimple.json"),
    )
    jobs = ashby.fetch({"type": "ashby", "company": "wealthsimple"})

    assert len(jobs) == 2  # the fixture's third posting is unlisted
    job = jobs[0]
    assert job.id.startswith("ashby:wealthsimple:")
    assert job.title and job.url and job.location
    assert job.company == "wealthsimple"
    assert job.source == "ashby/wealthsimple"
    assert len(job.description) <= 500
    assert "<" not in job.description  # plain text, no HTML
    assert all(j.title != "Hidden Posting" for j in jobs)


@responses.activate
def test_greenhouse_maps_jobs(fixture):
    responses.get(
        "https://boards-api.greenhouse.io/v1/boards/duolingo/jobs",
        json=fixture("greenhouse_duolingo.json"),
    )
    jobs = greenhouse.fetch({"type": "greenhouse", "company": "duolingo"})

    assert len(jobs) == 2
    job = jobs[0]
    assert job.id.startswith("greenhouse:duolingo:")
    assert job.title and job.url and job.location
    assert job.posted_at  # first_published or updated_at
    assert len(job.description) <= 500
    assert "&lt;" not in job.description  # double-escaped HTML fully unescaped
    assert "<" not in job.description


@responses.activate
def test_lever_maps_jobs(fixture):
    responses.get(
        "https://api.lever.co/v0/postings/palantir",
        json=fixture("lever_palantir.json"),
    )
    jobs = lever.fetch({"type": "lever", "company": "palantir"})

    assert len(jobs) == 2
    job = jobs[0]
    assert job.id.startswith("lever:palantir:")
    assert job.title and job.url and job.location
    assert job.posted_at and job.posted_at.startswith("20")  # ms epoch -> ISO
    assert len(job.description) <= 500


@responses.activate
def test_github_maps_active_visible_listings(fixture, tmp_path):
    url = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/l.json"
    responses.get(url, json=fixture("github_listings.json"), headers={"ETag": 'W/"abc"'})
    config = {
        "type": "github",
        "repo": "SimplifyJobs/New-Grad-Positions",
        "path": "l.json",
        "branch": "dev",
        "etag_cache_path": str(tmp_path / "etags.json"),
    }
    jobs = github_repo.fetch(config)

    assert len(jobs) == 2  # inactive and invisible listings skipped
    job = jobs[0]
    assert job.id.startswith("github:SimplifyJobs/New-Grad-Positions:")
    assert job.title and job.url and job.company
    assert all(j.title not in ("Old Job", "Hidden Job") for j in jobs)


@responses.activate
def test_github_304_returns_empty_without_parsing(fixture, tmp_path):
    url = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/l.json"
    cache_path = str(tmp_path / "etags.json")
    config = {
        "type": "github",
        "repo": "SimplifyJobs/New-Grad-Positions",
        "path": "l.json",
        "branch": "dev",
        "etag_cache_path": cache_path,
    }

    responses.get(url, json=fixture("github_listings.json"), headers={"ETag": 'W/"abc"'})
    assert len(github_repo.fetch(config)) == 2  # first run primes the cache

    responses.reset()
    responses.get(url, status=304)
    assert github_repo.fetch(config) == []
    # and the conditional header was actually sent
    assert responses.calls[0].request.headers["If-None-Match"] == 'W/"abc"'


WORKDAY_URL = "https://ngc.wd1.myworkdayjobs.com/wday/cxs/ngc/Northrop_Grumman_External_Site/jobs"
WORKDAY_CONFIG = {
    "type": "workday",
    "company": "northrop-grumman",
    "tenant": "ngc",
    "host": "wd1",
    "site": "Northrop_Grumman_External_Site",
}


@responses.activate
def test_workday_maps_jobs_and_parses_fuzzy_dates(fixture):
    responses.post(WORKDAY_URL, json=fixture("workday_ngc.json"))
    jobs = workday.fetch(WORKDAY_CONFIG)

    assert len(jobs) == 3
    job = jobs[0]
    assert job.id == "workday:ngc:R10238386"  # req id from bulletFields
    assert job.title and job.location
    assert job.company == "northrop-grumman"
    assert job.source == "workday/northrop-grumman"
    assert job.url.startswith(
        "https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site/job/"
    )
    today = datetime.now(UTC).date()
    assert jobs[0].posted_at == today.isoformat()  # "Posted Today"
    assert jobs[1].posted_at == (today - timedelta(days=3)).isoformat()  # "Posted 3 Days Ago"
    assert jobs[2].posted_at is None  # "Posted 30+ Days Ago": age unknown
    assert jobs[2].id.startswith("workday:ngc:/job/")  # no bulletFields: path fallback


@responses.activate
def test_workday_paginates_until_total(fixture):
    posting = fixture("workday_ngc.json")["jobPostings"][0]
    responses.post(WORKDAY_URL, json={"total": 23, "jobPostings": [posting] * 20})
    responses.post(WORKDAY_URL, json={"total": 23, "jobPostings": [posting] * 3})

    jobs = workday.fetch(WORKDAY_CONFIG)

    assert len(jobs) == 23
    assert len(responses.calls) == 2  # stopped at total, not at MAX_POSTINGS


ORACLE_URL = "https://dell.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
ORACLE_CONFIG = {
    "type": "oracle",
    "company": "dell",
    "host": "dell.oraclecloud.com",
    "site_number": "CX_1",
    "site_name": "careers",
}


@responses.activate
def test_oracle_maps_jobs(fixture):
    responses.get(ORACLE_URL, json=fixture("oracle_dell.json"))
    jobs = oracle.fetch(ORACLE_CONFIG)

    assert len(jobs) == 2
    assert len(responses.calls) == 1  # TotalJobsCount reached, no needless page
    job = jobs[0]
    assert job.id == "oracle:dell:R290099"
    assert job.title == "Software Engineer"
    assert job.company == "dell"
    assert job.source == "oracle/dell"
    # apply URL uses the site *name*, not the API site number
    assert job.url == (
        "https://dell.oraclecloud.com/hcmUI/CandidateExperience/en/sites/careers/job/R290099"
    )
    assert job.posted_at == "2026-07-24"
    assert job.location == "Austin, Texas, United States (+1 more)"
    assert "<" not in job.description  # HTML stripped
    assert "Java" in job.description
    assert jobs[1].location == "Toronto, Ontario, Canada"  # no secondary, no suffix


@responses.activate
def test_oracle_paginates_until_total(fixture):
    posting = fixture("oracle_dell.json")["items"][0]["requisitionList"][0]
    responses.get(
        ORACLE_URL,
        json={"items": [{"TotalJobsCount": 30, "requisitionList": [posting] * 25}]},
    )
    responses.get(
        ORACLE_URL,
        json={"items": [{"TotalJobsCount": 30, "requisitionList": [posting] * 5}]},
    )

    jobs = oracle.fetch(ORACLE_CONFIG)

    assert len(jobs) == 30
    assert len(responses.calls) == 2  # stopped at total, not at MAX_POSTINGS


# --- extensions to existing adapters ---------------------------------------------


@responses.activate
def test_lever_eu_boards_use_the_eu_api_host(fixture):
    responses.get("https://api.eu.lever.co/v0/postings/cirrus", json=fixture("lever_palantir.json"))
    jobs = lever.fetch({"company": "cirrus", "region": "eu"})
    assert jobs and jobs[0].id.startswith("lever:cirrus:")


@responses.activate
def test_workday_site_domain_puts_the_tenant_in_the_path(fixture):
    api = "https://wd5.myworkdaysite.com/wday/cxs/microchiphr/External/jobs"
    responses.post(api, json=fixture("workday_ngc.json"))
    jobs = workday.fetch(
        {"tenant": "microchiphr", "host": "wd5", "site": "External",
         "domain": "myworkdaysite.com"}
    )
    assert jobs[0].url.startswith("https://wd5.myworkdaysite.com/recruiting/microchiphr/External/")


# --- new adapters ------------------------------------------------------------------


@responses.activate
def test_smartrecruiters_maps_jobs_and_narrows_by_country_and_query(fixture):
    url = "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"
    responses.get(url, json=fixture("smartrecruiters_bosch.json"))
    jobs = smartrecruiters.fetch(
        {"company": "BoschGroup", "countries": ["us"], "query": "engineer"}
    )

    assert len(jobs) == 3
    job = jobs[0]
    assert job.id == "smartrecruiters:BoschGroup:744000151928744"
    assert job.title == "Design Engineer"
    assert job.company == "Bosch Group"
    assert job.location == "Pleasanton, CA, United States"
    assert job.url == "https://jobs.smartrecruiters.com/BoschGroup/744000151928744"
    assert job.posted_at == "2026-09-25T20:04:47.428Z"
    params = responses.calls[0].request.params
    assert params["country"] == "us" and params["q"] == "engineer"
    assert not newest_first(smartrecruiters.fetch)


@responses.activate
def test_smartrecruiters_pages_and_dedups_across_countries(fixture):
    url = "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"
    page = fixture("smartrecruiters_bosch.json")
    page["totalFound"] = 3
    responses.get(url, json=page)  # us
    responses.get(url, json=page)  # ca: the same reqs, listed again
    jobs = smartrecruiters.fetch({"company": "BoschGroup", "countries": ["us", "ca"]})
    assert len(jobs) == 3
    assert len(responses.calls) == 2  # short page: no needless second page per country


@responses.activate
def test_workable_maps_jobs_and_follows_the_page_token(fixture):
    url = "https://apply.workable.com/api/v3/accounts/tickpick/jobs"
    first = fixture("workable_tickpick.json") | {"nextPage": "abc"}
    responses.post(url, json=first)
    responses.post(url, json=fixture("workable_tickpick.json"))
    jobs = workable.fetch({"company": "tickpick"})

    assert len(jobs) == 6
    job = jobs[0]
    assert job.id == "workable:tickpick:F4AEB07453"
    assert job.title == "General Counsel"
    assert job.location == "New York, New York, United States"
    assert job.url == "https://apply.workable.com/tickpick/j/F4AEB07453/"
    assert job.posted_at == "2026-09-16T00:00:00.000Z"
    assert json.loads(responses.calls[1].request.body)["token"] == "abc"


@responses.activate
def test_rippling_maps_jobs(fixture):
    responses.get(
        "https://api.rippling.com/platform/api/ats/v1/board/flexai/jobs",
        json=fixture("rippling_flexai.json"),
    )
    [job, *_] = rippling.fetch({"company": "flexai"})
    assert job.id == "rippling:flexai:d2b49eda-99ee-4f4d-ba53-677e7f5360c2"
    assert job.title == "Senior Backend Engineer"
    assert job.location == "Bangalore, India"
    assert job.url == "https://ats.rippling.com/flexai/jobs/d2b49eda-99ee-4f4d-ba53-677e7f5360c2"
    assert job.posted_at is None


@responses.activate
def test_bamboohr_maps_jobs(fixture):
    responses.get(
        "https://lexical.bamboohr.com/careers/list", json=fixture("bamboohr_lexical.json")
    )
    [job, *_] = bamboohr.fetch({"company": "lexical"})
    assert job.id == "bamboohr:lexical:70"
    assert job.title == "NLM Cloud Engineer I"
    assert job.location == "Bethesda, Maryland"
    assert job.url == "https://lexical.bamboohr.com/careers/70"
    assert responses.calls[0].request.headers["Accept"] == "application/json"


@responses.activate
def test_jibe_maps_jobs_newest_first(fixture):
    responses.get("https://careers.amd.com/api/jobs", json=fixture("jibe_amd.json"))
    jobs = jibe.fetch({"company": "amd", "host": "careers.amd.com"})

    assert len(jobs) == 3
    assert len(responses.calls) == 1  # short page: done
    job = jobs[0]
    assert job.id == "jibe:careers.amd.com:92773"
    assert job.title == "Business Operations Budget Manager"
    assert job.location == "Austin, Texas"
    assert job.url == "https://careers.amd.com/jobs/92773"
    assert job.posted_at == "2026-09-27T16:36:00+00:00"  # "+0000" normalised
    assert "<" not in job.description and job.description.startswith("ADVANCE YOUR CAREER")
    assert responses.calls[0].request.params["sortBy"] == "posted_date"


@responses.activate
def test_successfactors_maps_rss_items():
    with open("tests/fixtures/successfactors_l3harris.xml", "rb") as f:
        responses.get("https://jobs.l3harris.com/services/rss/job/", body=f.read())
    jobs = successfactors.fetch({"company": "l3harris", "host": "jobs.l3harris.com"})

    assert len(jobs) == 3
    job = jobs[0]
    assert job.id == "successfactors:jobs.l3harris.com:1434203000"
    assert job.title == "Manager, Manufacturing Engineering (Digital Tools and SPC)"
    assert job.location == "Camden, AR, US"  # split off the title, zip dropped
    assert job.url.endswith("/1434203000/") and "utm_" not in job.url
    assert job.posted_at == "2026-09-28T00:00:00+00:00"
    assert job.description.startswith("Job Title: Manager")
    assert responses.calls[0].request.params["keywords"] == ""  # "()" breaks the feed


@responses.activate
def test_successfactors_feed_error_raises():
    responses.get(
        "https://jobs.l3harris.com/services/rss/job/",
        body="<xml>Error: There is a problem with a jobs query</xml>",
    )
    try:
        successfactors.fetch({"company": "l3harris", "host": "jobs.l3harris.com"})
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "feed error" in str(exc)


@responses.activate
def test_eightfold_maps_jobs(fixture):
    responses.get("https://qualcomm.eightfold.ai/api/pcsx/search",
                  json=fixture("eightfold_qualcomm.json"))
    jobs = eightfold.fetch({"company": "qualcomm", "tenant": "qualcomm", "domain": "qualcomm.com"})

    job = jobs[0]
    assert job.id == "eightfold:qualcomm:446721255550"
    assert job.title == "Engineer - BT/UWB validation and system integration"
    assert job.location == "Bangalore, India"
    assert job.url == "https://qualcomm.eightfold.ai/careers/job/446721255550"
    assert job.posted_at == "2026-09-26T00:00:00+00:00"
    assert responses.calls[0].request.params["domain"] == "qualcomm.com"


@responses.activate
def test_tiktok_maps_jobs_and_dates_them_from_the_snowflake_id(fixture):
    responses.post(tiktok.API_URL, json=fixture("tiktok.json"))
    jobs = tiktok.fetch({})

    job = jobs[0]
    assert job.id == "tiktok:tiktok:7686714309884578101"
    assert job.title == "Workplace Manager"
    assert job.location == "San Jose, California, United States of America"
    assert job.url == "https://lifeattiktok.com/search/7686714309884578101"
    assert job.posted_at == "2026-09-18T03:37:20+00:00"
    assert responses.calls[0].request.headers["website-path"] == "tiktok"
    assert not newest_first(tiktok.fetch)


@responses.activate
def test_tiktok_error_code_raises():
    responses.post(tiktok.API_URL, json={"code": -1, "data": None, "message": "bad"})
    try:
        tiktok.fetch({})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@responses.activate
def test_amazon_maps_jobs_filtered_to_north_america(fixture):
    responses.get(amazon.API_URL, json=fixture("amazon.json"))
    jobs = amazon.fetch({})

    job = jobs[0]
    assert job.id == "amazon:amazon:10560963"
    assert job.title == "Data Center Technician , DCC Communities"
    assert job.company == "amazon"
    assert job.location == "Gilroy, California, USA"
    assert job.url == "https://www.amazon.jobs/en/jobs/10560963/data-center-technician-dcc-communities"
    assert job.posted_at == "2026-09-25"
    query = responses.calls[0].request.url
    assert re.search(r"normalized_country_code%5B%5D=USA.*normalized_country_code%5B%5D=CAN", query)
    assert "sort=recent" in query


@responses.activate
def test_tiktok_filters_by_recruitment_type_and_warns_when_capped(fixture, monkeypatch, caplog):
    posting = fixture("tiktok.json")["data"]["job_post_list"][0]
    monkeypatch.setattr(tiktok, "MAX_POSTINGS", 2 * tiktok.PAGE_SIZE)
    page = {"code": 0, "data": {"count": 5000, "job_post_list": [posting] * tiktok.PAGE_SIZE}}
    responses.post(tiktok.API_URL, json=page)
    responses.post(tiktok.API_URL, json=page)

    tiktok.fetch({"recruitment_ids": [2]})

    assert json.loads(responses.calls[0].request.body)["recruitment_id_list"] == ["2"]
    assert len(responses.calls) == 2  # stopped at the cap
    assert "exceed the 200 cap" in caplog.text


@responses.activate
def test_eightfold_on_a_company_domain_uses_that_host(fixture):
    responses.get("https://apply.careers.microsoft.com/api/pcsx/search",
                  json=fixture("eightfold_qualcomm.json"))
    jobs = eightfold.fetch({"company": "microsoft", "tenant": "microsoft",
                            "host": "apply.careers.microsoft.com", "domain": "microsoft.com"})
    assert jobs[0].id == "eightfold:microsoft:446721255550"
    assert jobs[0].url == "https://apply.careers.microsoft.com/careers/job/446721255550"
    assert responses.calls[0].request.params["domain"] == "microsoft.com"


@responses.activate
def test_workday_keeps_paging_past_the_cap_while_pages_hold_new_postings(fixture, monkeypatch):
    # TD-style ordering: a block of fresh postings runs across the cap.
    monkeypatch.setattr(workday, "MAX_POSTINGS", 40)
    posting = fixture("workday_ngc.json")["jobPostings"][0]
    fresh = posting | {"postedOn": "Posted Today"}
    old = posting | {"postedOn": "Posted 30+ Days Ago"}
    for page in ([fresh] * 20, [fresh] * 20, [fresh] * 20, [old] * 20, [old] * 20):
        responses.post(WORKDAY_URL, json={"total": 1706, "jobPostings": page})

    jobs = workday.fetch(WORKDAY_CONFIG)

    assert len(jobs) == 80  # read on past the cap until a page held nothing fresh
    assert len(responses.calls) == 4


@responses.activate
def test_workday_ordered_board_still_stops_at_the_cap(fixture, monkeypatch):
    monkeypatch.setattr(workday, "MAX_POSTINGS", 40)
    posting = fixture("workday_ngc.json")["jobPostings"][0]
    old = posting | {"postedOn": "Posted 5 Days Ago"}
    for _ in range(4):
        responses.post(WORKDAY_URL, json={"total": 1706, "jobPostings": [old] * 20})
    assert len(workday.fetch(WORKDAY_CONFIG)) == 40
    assert len(responses.calls) == 2
