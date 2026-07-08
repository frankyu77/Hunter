"""Adapter mapping tests. All HTTP is mocked from recorded fixtures - no
network access. Each test asserts the fixture's real payload maps to the
canonical Job shape, including the skip rules."""

from datetime import UTC, datetime, timedelta

import responses

from scraper.adapters import REGISTRY, ashby, get_adapter, github_repo, greenhouse, lever, workday


def test_registry_dispatches_all_types():
    for type_str in ["ashby", "greenhouse", "lever", "github", "workday"]:
        assert callable(get_adapter(type_str))


def test_registry_rejects_unknown_type():
    try:
        get_adapter("taleo")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert "taleo" in str(exc)
        assert "ashby" in str(exc)  # error names the known types


def test_registry_has_no_stale_entries():
    assert set(REGISTRY) == {"ashby", "greenhouse", "lever", "github", "workday"}


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
