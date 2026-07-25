from scraper.models import Job
from scraper.notify import categorize, format_digest, format_message
from scraper.notify import _region


def make_job(
    n: int = 1, title: str | None = None, location: str = "Remote"
) -> Job:
    return Job(
        id=f"x:y:{n}",
        title=title or f"Engineer {n} <Platform & Tools>",
        company="Acme",
        location=location,
        url=f"https://example.com/jobs/{n}?a=1&b=2",
        posted_at="2026-06-17T20:31:02.329+00:00",
        description="Build <great> things & more.",
        source="x/y",
    )


def test_message_escapes_html_and_trims_date():
    text = format_message(make_job())
    assert "&lt;Platform &amp; Tools&gt;" in text
    assert "Posted: 2026-06-17" in text
    assert "20:31" not in text  # date only, no timestamp
    assert 'href="https://example.com/jobs/1?a=1&amp;b=2"' in text


def test_message_omits_description():
    text = format_message(make_job())
    assert "Build" not in text  # the raw description is not reported


def test_message_frames_title_for_skimming():
    text = format_message(make_job(title="Software Intern"))
    assert text.startswith("<b>🌱 Software Intern</b>")


def test_message_reports_company_location_and_extracted_fields():
    job = Job(
        id="x:y:1",
        title="Backend Engineer",
        company="Acme",
        location="Toronto, Canada",
        url="https://example.com/jobs/1",
        posted_at="2026-06-17T20:31:02+00:00",
        description="We want strong Python and Java skills, OOP, and REST. "
        "Compensation is $120,000 - $150,000 per year.",
        source="x/y",
    )
    text = format_message(job)
    assert "Acme — Toronto, Canada" in text
    assert "Pay: $120,000 - $150,000" in text
    assert "Keywords: Python, Java, OOP, REST" in text


def test_categorize_buckets_by_title():
    assert categorize(make_job(title="Software Engineering Intern")) == "internship"
    assert categorize(make_job(title="Co-op Developer")) == "internship"
    assert categorize(make_job(title="New Grad Software Engineer")) == "new_grad"
    assert categorize(make_job(title="Junior Backend Engineer")) == "new_grad"
    assert categorize(make_job(title="Software Engineer 1")) == "new_grad"
    assert categorize(make_job(title="Software Engineer I")) == "new_grad"
    assert categorize(make_job(title="Software Engineer II")) == "full_time"
    assert categorize(make_job(title="Staff Software Engineer")) == "full_time"
    assert categorize(make_job(title="AI Prompt Engineer")) == "full_time"


def test_digest_lists_jobs_and_counts():
    jobs = [make_job(n) for n in range(1, 4)]
    messages = format_digest(jobs)
    assert len(messages) == 1
    assert messages[0].startswith("<b>💼 FULL-TIME (3)</b>")
    assert messages[0].count("<a href=") == 3


def test_digest_shows_company_and_location():
    messages = format_digest([make_job(1)])
    assert "— Acme · Remote" in messages[0]


def test_region_classifies_locations():
    assert _region("Toronto, Canada") == "canada"
    assert _region("Vancouver, BC") == "canada"
    assert _region("Waterloo, ON") == "canada"
    assert _region("Ottawa") == "canada"
    assert _region("Seattle, WA") == "us"
    assert _region("Redmond, Washington, United States") == "us"  # full name, capitalized
    assert _region("USA - Oklahoma City, OK") == "us"
    assert _region("New York, NY") == "us"
    assert _region("Remote - US") == "us"
    assert _region("U.S.") == "us"
    assert _region("Waterloo, IA") == "us"  # state code wins over city name
    assert _region("London, UK") == "other"
    assert _region("Remote") == "other"
    assert _region("") == "other"


def test_region_ignores_english_words_as_state_codes():
    # "or"/"in"/"me" must not be read as Oregon/Indiana/Maine.
    assert _region("Remote or hybrid") == "other"
    assert _region("Work in office") == "other"


def test_digest_groups_by_region_with_flags():
    jobs = [
        make_job(1, location="Toronto, Canada"),
        make_job(2, location="Seattle, WA"),
        make_job(3, location="London, UK"),
        make_job(4, location="Vancouver, BC"),
    ]
    [message] = format_digest(jobs)
    assert message.startswith("<b>💼 FULL-TIME (4)</b>")
    assert "<b>🇨🇦 Canada (2)</b>" in message
    assert "<b>🇺🇸 United States (1)</b>" in message
    assert "<b>🌐 Other (1)</b>" in message
    # region order: Canada before US before Other
    assert message.index("🇨🇦") < message.index("🇺🇸") < message.index("🌐")


def test_digest_omits_empty_regions():
    messages = format_digest([make_job(1, location="Toronto, Canada")])
    assert "🇨🇦 Canada (1)" in messages[0]
    assert "United States" not in messages[0]
    assert "Other" not in messages[0]


def test_digest_separates_categories_into_own_messages():
    jobs = [
        make_job(1, title="Software Engineering Intern"),
        make_job(2, title="New Grad Software Engineer"),
        make_job(3, title="Staff Software Engineer"),
        make_job(4, title="Infrastructure Intern"),
    ]
    messages = format_digest(jobs)
    assert len(messages) == 3
    assert messages[0].startswith("<b>🌱 INTERNSHIPS (2)</b>")
    assert messages[1].startswith("<b>🎓 NEW GRAD & JUNIOR (1)</b>")
    assert messages[2].startswith("<b>💼 FULL-TIME (1)</b>")
    assert messages[0].count("<a href=") == 2


def test_digest_splits_into_multiple_messages_under_telegram_cap():
    jobs = [make_job(n, title="X" * 200) for n in range(100)]
    messages = format_digest(jobs)
    assert len(messages) > 1
    assert all(len(m) <= 4000 for m in messages)
    # every job appears exactly once across all messages
    assert sum(m.count("<a href=") for m in messages) == 100
    assert messages[1].startswith("<b>💼 FULL-TIME (100)</b> (continued)")
