"""Telegram sender.

Posts one message per job - or per group of identical-looking postings -
via the Bot API sendMessage endpoint (HTML parse mode). Credentials come
only from environment variables, injected by GitHub Actions Secrets - never
from config files.

Every job message carries an inline ✅ button (👍/👎 moved to the
dashboard; stars were removed). This module only renders it and exposes
the raw Bot API calls; what a press *means* lives in scraper.feedback.
Callback data is "<action>:<token>", where the token is a
short hash of the job id - Telegram caps callback data at 64 bytes, and
aggregator ids alone run longer than that.
"""

import hashlib
import html
import logging
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus

import requests

from scraper.models import Job, JobNotes
from scraper.regions import region

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
SEND_PAUSE_SECONDS = 0.5  # stay well under Telegram's rate limits
TIMEOUT_SECONDS = 30

# Seniority buckets, in the order their digest messages are sent.
CATEGORIES = (
    ("internship", "🌱", "INTERNSHIPS"),
    ("new_grad", "🎓", "NEW GRAD & JUNIOR"),
    ("full_time", "💼", "FULL-TIME"),
)
_EMOJI = {key: emoji for key, emoji, _ in CATEGORIES}

# Within each seniority digest, jobs are further split by region, in this order.
REGIONS = (
    ("canada", "🇨🇦", "Canada"),
    ("us", "🇺🇸", "United States"),
    ("other", "🌐", "Other"),
)

_INTERN_RE = re.compile(r"\bintern(ship)?\b|\bco[-\s]?op\b|\bstudent\b", re.IGNORECASE)
# Internship feeds list student roles whose titles say nothing about it
# ("Student Web Developer"), so the feed itself is the stronger signal.
_INTERN_FEED_RE = re.compile(r"intern", re.IGNORECASE)
# "Engineer I" / "Engineer 1" style level suffixes count as junior; II+ do not.
_NEW_GRAD_RE = re.compile(
    r"\bnew\s+grad(uate)?\b|\bgraduate\b|\bentry[-\s]level\b|\bearly\s+career\b"
    r"|\bjunior\b|\bjr\.?\b|\bassociate\b|\bcampus\b|\buniversity\s+grad"
    r"|\b(i|1)\s*$",
    re.IGNORECASE,
)


def categorize(job: Job) -> str:
    if _INTERN_RE.search(job.title):
        return "internship"
    repo = _github_repo(job.source)
    if repo and _INTERN_FEED_RE.search(repo):
        return "internship"
    if _NEW_GRAD_RE.search(job.title):
        return "new_grad"
    return "full_time"


# Company names reach notify in two shapes: bare ATS slugs from sources.yaml
# ("td", "northrop-grumman") and already-cased names from the aggregator feeds
# ("Domino Data Lab"). Slugs get title-cased; anything already carrying an
# uppercase letter is trusted as-is. Only names the generic rule gets wrong -
# acronyms and camel-cased brands - need an entry below.
_COMPANY_NAMES = {
    "cibc": "CIBC",
    "gitlab": "GitLab",
    "hp": "HP",
    "hpe": "HPE",
    "ibm": "IBM",
    "janestreet": "Jane Street",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "rbc": "RBC",
    "td": "TD",
}


def display_company(company: str) -> str:
    name = company.strip()
    override = _COMPANY_NAMES.get(name.lower())
    if override:
        return override
    if any(char.isupper() for char in name):
        return name
    return re.sub(r"[-_]+", " ", name).title()


_GITHUB_PREFIX = "github/"
GITHUB_REPO_URL = "https://github.com/{repo}"


def _github_repo(source: str) -> str | None:
    """The repo slug when a job came from a GitHub aggregator feed, else None."""
    if source.startswith(_GITHUB_PREFIX):
        return source[len(_GITHUB_PREFIX) :]
    return None


def group_duplicates(jobs: list[Job]) -> list[list[Job]]:
    """Collapse postings that look identical to a reader - same company,
    title and location under different ATS ids (one role opened as several
    reqs). Order is preserved. Display-only: every job keeps its own id and
    is still recorded as seen individually."""
    groups: dict[tuple[str, str, str], list[Job]] = {}
    for job in jobs:
        key = (
            display_company(job.company).casefold(),
            " ".join(job.title.split()).casefold(),
            " ".join(job.location.split()).casefold(),
        )
        groups.setdefault(key, []).append(job)
    return list(groups.values())


def send(
    job: Job, copies: int = 1, notes: JobNotes | None = None, school: str | None = None
) -> None:
    _post(format_message(job, copies, notes, school), keyboard([callback_token(job.id)]))
    log.info("Notified: %s", job.id)
    time.sleep(SEND_PAUSE_SECONDS)


def send_digest(jobs: list[Job], notes: dict[str, JobNotes] | None = None) -> None:
    messages = digest_messages(jobs, notes)
    for text, tokens in messages:
        _post(text, keyboard(tokens, numbered=True))
        time.sleep(SEND_PAUSE_SECONDS)
    log.info("Notified: digest of %d jobs in %d message(s)", len(jobs), len(messages))


def send_text(text: str) -> None:
    """Send a plain (non-job) message, e.g. a health warning."""
    _post(html.escape(text))
    log.info("Notified: %s", text)


def send_html(text: str) -> None:
    """Send an already-formatted message (season / closure alerts)."""
    _post(text)
    log.info("Notified: %s", text.splitlines()[0])
    time.sleep(SEND_PAUSE_SECONDS)


def _post(text: str, markup: dict | None = None) -> None:
    payload = {
        "chat_id": chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    call("sendMessage", payload)


def chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"].strip()


def call(method: str, payload: dict) -> object:
    """POST one Bot API method and return its ``result``."""
    # Strip whitespace: a token pasted into GitHub Secrets with a trailing
    # newline becomes %0A in the URL and Telegram answers 404.
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    response = requests.post(
        API_URL.format(token=token, method=method), json=payload, timeout=TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json().get("result")


# --- buttons ------------------------------------------------------------------

# "u"/"d" (👍/👎) are no longer drawn - voting moved to the dashboard - but
# messages sent before that still carry them, so their presses still count.
ACTIONS = {"u": "up", "d": "down", "a": "applied"}
# Buttons old messages still carry for features that no longer exist: a
# press is answered with this, and changes nothing.
RETIRED = {"s": "Stars were removed from Hunter - nothing to do."}


def callback_token(job_id: str) -> str:
    return hashlib.sha256(job_id.encode()).hexdigest()[:12]


def button_row(
    token: str,
    number: int | None = None,
    applied: bool = False,
) -> list[dict]:
    """One row for one job - just ✅ now; ✓ marks the current state. Digest
    rows carry the entry number so each row maps to a line of the message.
    👍/👎 live on the dashboard only."""
    prefix = f"{number} " if number else ""
    done = "✅" if number else "✅ Applied"
    labels = {"a": f"{prefix}{done}{' ✓' if applied else ''}"}
    return [{"text": label, "callback_data": f"{a}:{token}"} for a, label in labels.items()]


def keyboard(tokens: list[str], numbered: bool = False) -> dict:
    rows = [button_row(t, i + 1 if numbered else None) for i, t in enumerate(tokens)]
    return {"inline_keyboard": rows}


_ROW_NUMBER = re.compile(r"^(\d+) ")


def restyle(markup: dict, token: str, applied: bool) -> dict:
    """The message's keyboard with ``token``'s row redrawn for its new state.
    Rebuilt from the keyboard Telegram echoes back with each press, so no
    per-message layout needs to be stored."""
    rows = []
    for row in markup.get("inline_keyboard", []):
        if any(button.get("callback_data", "").endswith(f":{token}") for button in row):
            number = _ROW_NUMBER.match(row[0].get("text", ""))
            row = button_row(
                token, int(number.group(1)) if number else None, applied
            )
        rows.append(row)
    return {"inline_keyboard": rows}


def get_updates(offset: int | None) -> list[dict]:
    payload: dict = {"timeout": 0, "allowed_updates": ["callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return call("getUpdates", payload) or []


def edit_keyboard(chat: int | str, message_id: int, markup: dict) -> None:
    call("editMessageReplyMarkup", {"chat_id": chat, "message_id": message_id,
                                    "reply_markup": markup})


def answer_callback(callback_id: str, text: str) -> None:
    call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def format_message(
    job: Job, copies: int = 1, notes: JobNotes | None = None, school: str | None = None
) -> str:
    # Telegram HTML mode breaks on unescaped <, >, & - escape everything
    # that originates from the source.
    e = html.escape
    lines = [f"<b>{_EMOJI[categorize(job)]} {e(job.title)}</b>{_copies(copies)}"]

    company_line = e(display_company(job.company))
    if job.location:
        company_line += f" — {e(job.location)}"
    lines.append(company_line)

    if job.posted_at:
        posted = _date_only(job.posted_at)
        age = _age(job.posted_at)
        lines.append(f"Posted: {e(posted)} ({age})" if age else f"Posted: {e(posted)}")

    # No dedicated pay/keyword fields exist, so mine them from the
    # description; both are omitted when nothing recognisable is found.
    pay = _extract_pay(job.description)
    if pay:
        lines.append(f"Pay: {e(pay)}")
    keywords = _extract_keywords(job.description)
    if keywords:
        lines.append(f"Keywords: {e(', '.join(keywords))}")

    if notes and notes.reposted_since:
        lines.append(f"🔁 Reposted: first listed {e(notes.reposted_since)}, closed unfilled")
    if notes and notes.typical_open_days is not None:
        company = e(display_company(job.company))
        lines.append(f"⏳ {company} postings usually stay open {_days(notes.typical_open_days)}")

    lines.append("")
    apply = f'<a href="{e(job.url, quote=True)}">Apply</a>'
    repo = _github_repo(job.source)
    if repo:
        url = e(GITHUB_REPO_URL.format(repo=repo), quote=True)
        apply += f' · via <a href="{url}">{e(repo)}</a>'
    lines.append(apply)
    lines.append(company_links(job.company, school))
    return "\n".join(lines)


# Research links are searches or slug guesses built from the display name;
# no lookups happen at send time. levels.fyi slugs are the lowercased,
# hyphenated name, which holds for most companies - Glassdoor and LinkedIn
# are keyword searches, so they always land somewhere useful.
LEVELS_URL = "https://www.levels.fyi/companies/{slug}/salaries"
GLASSDOOR_URL = "https://www.glassdoor.com/Search/results.htm?keyword={query}"
LINKEDIN_URL = "https://www.linkedin.com/search/results/people/?keywords={query}"


def company_links(company: str, school: str | None = None) -> str:
    """One line of one-tap research links: pay, reviews, and people to ask
    for a referral (alumni of ``school`` when configured)."""
    e = html.escape
    name = display_company(company)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    people = f"{name} {school}" if school else name
    links = [
        (LEVELS_URL.format(slug=quote(slug)), "levels.fyi"),
        (GLASSDOOR_URL.format(query=quote_plus(name)), "Glassdoor"),
        (LINKEDIN_URL.format(query=quote_plus(people)), "LinkedIn"),
    ]
    return "🔗 " + " · ".join(f'<a href="{e(url, quote=True)}">{label}</a>' for url, label in links)


_SEASON_NAMES = {"internship": "Internships · Summer {year}", "new_grad": "New Grad {year}"}


def format_season_alert(job: Job, category: str, year: int) -> str:
    e = html.escape
    season = _SEASON_NAMES[category].format(year=year)
    return "\n".join(
        [
            f"🚨 <b>{e(display_company(job.company))} opened {e(season)}</b>",
            f'First posting: <a href="{e(job.url, quote=True)}">{e(job.title)}</a>',
        ]
    )


def format_digest(jobs: list[Job], notes: dict[str, JobNotes] | None = None) -> list[str]:
    return [text for text, _ in digest_messages(jobs, notes)]


def digest_messages(
    jobs: list[Job], notes: dict[str, JobNotes] | None = None
) -> list[tuple[str, list[str]]]:
    """(text, callback tokens of its numbered entries) per digest message.

    One message (or more, if long) per seniority bucket, so internships,
    new-grad roles, and full-time roles never share a message. Counts in
    the headers are entries, i.e. duplicates collapsed."""
    messages: list[tuple[str, list[str]]] = []
    entries = group_duplicates(jobs)
    for key, emoji, name in CATEGORIES:
        group = [dupes for dupes in entries if categorize(dupes[0]) == key]
        if group:
            messages.extend(_format_group(f"{emoji} {name}", group, notes or {}))
    return messages


CAP = 3800  # stay safely under Telegram's 4096-char message cap
# One button row per entry: 15 rows x 4 buttons keeps each message's
# keyboard well inside Telegram's ~100-button limit and short enough to scroll.
MAX_ENTRIES = 15


def _format_entry(dupes: list[Job], notes: dict[str, JobNotes]) -> str:
    """One digest entry: company first and bold, then location and age, then
    feed. Duplicates share one entry, linked to the first posting.

    Returned as a single multi-line string so the message splitter treats the
    entry as one indivisible unit and never orphans a job's location line.
    Research links are left to single messages: three URLs per entry would
    roughly halve how many entries fit under the cap.
    """
    e = html.escape
    job = dupes[0]
    note = next((notes[dupe.id] for dupe in dupes if dupe.id in notes), None)
    repost = " 🔁" if note and note.reposted_since else ""
    lines = [
        f'<b>{e(display_company(job.company))}</b> — '
        f'<a href="{e(job.url, quote=True)}">{e(job.title)}</a>{_copies(len(dupes))}{repost}'
    ]
    details = [e(job.location)] if job.location else []
    if job.posted_at and (age := _age(job.posted_at)):
        details.append(age)
    if note and note.typical_open_days is not None:
        details.append(f"usually open {_days(note.typical_open_days)}")
    if details:
        lines.append(f"  {' · '.join(details)}")
    # Aggregator feeds pull from hundreds of companies, so naming the repo is
    # the only way to tell where a listing actually came from. Per-company ATS
    # sources are already identified by the company name above.
    repo = _github_repo(job.source)
    if repo:
        url = e(GITHUB_REPO_URL.format(repo=repo), quote=True)
        lines.append(f'  via <a href="{url}">{e(repo)}</a>')
    return "\n".join(lines)


def _format_group(
    label: str, jobs: list[list[Job]], notes: dict[str, JobNotes]
) -> list[tuple[str, list[str]]]:
    # Within the seniority group, split further by region (Canada / US /
    # Other), each under its own flagged subheader. Long groups spill across
    # as many messages as needed - each stays under the Telegram cap and
    # MAX_ENTRIES, and the main and region headers repeat on continuation so
    # no job is orphaned. Entries are numbered per message to match the
    # button rows under it.
    header = f"<b>{label} ({len(jobs)})</b>"

    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for key, flag, name in REGIONS:
        group = [dupes for dupes in jobs if region(dupes[0].location) == key]
        if not group:
            continue
        subheader = f"<b>{flag} {name} ({len(group)})</b>"
        entries = [(_format_entry(d, notes), callback_token(d[0].id)) for d in group]
        sections.append((subheader, entries))

    messages: list[tuple[str, list[str]]] = []
    lines = [header, ""]
    tokens: list[str] = []

    def used() -> int:
        return sum(len(line) + 1 for line in lines)

    def full(extra: int) -> bool:
        return bool(tokens) and (used() + extra > CAP or len(tokens) >= MAX_ENTRIES)

    def flush() -> None:
        nonlocal lines, tokens
        messages.append(("\n".join(lines), tokens))
        lines = [f"{header} (continued)", ""]
        tokens = []

    for subheader, entries in sections:
        # Keep a subheader with its first job: start a fresh message if the
        # pair won't fit on the current one.
        first = _numbered(len(tokens) + 1, entries[0][0])
        if full(1 + len(subheader) + 1 + len(first) + 1):
            flush()
        if tokens:
            lines.append("")
        lines.append(subheader)
        for entry, token in entries:
            if full(len(_numbered(len(tokens) + 1, entry)) + 1):
                flush()
                lines.append(subheader)
            lines.append(_numbered(len(tokens) + 1, entry))
            tokens.append(token)
    messages.append(("\n".join(lines), tokens))
    return messages


def _numbered(number: int, entry: str) -> str:
    return f"{number}. {entry}"


def _days(days: int) -> str:
    return "under a day" if days < 1 else f"~{days}d"


def _copies(count: int) -> str:
    return f" ×{count}" if count > 1 else ""


def _age(posted_at: str, now: datetime | None = None) -> str | None:
    """How long ago a job was posted, in whole days - what actually matters
    when deciding whether to apply now. None if the date can't be parsed."""
    try:
        posted = datetime.fromisoformat(posted_at)
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    days = (now.date() - posted.astimezone(UTC).date()).days
    if days <= 0:
        return "🔥 today"
    return f"{days}d ago"


def _date_only(posted_at: str) -> str:
    try:
        return datetime.fromisoformat(posted_at).date().isoformat()
    except ValueError:
        return posted_at


# A salary-like dollar amount: comma-grouped ($120,000), K/M-suffixed
# ($120K), or a bare run of 4+ digits ($120000). The leading amount must be
# $-anchored to avoid matching stray numbers; the second half of a range may
# drop the $ ("$120,000 - 150,000").
_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?[KkMm]\b|\d{4,})"
_PAY_RE = re.compile(
    rf"\$\s?{_AMOUNT}(?:\s?(?:-|–|—|to)\s?\$?\s?{_AMOUNT})?"
)


def _extract_pay(description: str) -> str | None:
    match = _PAY_RE.search(description)
    if not match:
        return None
    return " ".join(match.group(0).split())


# Recognisable skills/technologies, mapped to a canonical display form. Values
# are case-insensitive regexes; multiple spellings collapse onto one label.
_SKILLS = {
    "Python": r"\bpython\b",
    "Java": r"\bjava\b",
    "JavaScript": r"\bjavascript\b",
    "TypeScript": r"\btypescript\b",
    "C++": r"c\+\+",
    "C#": r"c#",
    "Golang": r"\bgolang\b",
    "Rust": r"\brust\b",
    "Ruby": r"\bruby\b",
    "Kotlin": r"\bkotlin\b",
    "Swift": r"\bswift\b",
    "Scala": r"\bscala\b",
    "PHP": r"\bphp\b",
    "MATLAB": r"\bmatlab\b",
    "SQL": r"\bsql\b",
    "NoSQL": r"\bnosql\b",
    "React": r"\breact(?:\.js)?\b",
    "Angular": r"\bangular\b",
    "Vue": r"\bvue(?:\.js)?\b",
    "Node.js": r"\bnode\.js\b",
    "Django": r"\bdjango\b",
    "Flask": r"\bflask\b",
    "Spring": r"\bspring\b",
    ".NET": r"\.net\b",
    "TensorFlow": r"\btensorflow\b",
    "PyTorch": r"\bpytorch\b",
    "Kafka": r"\bkafka\b",
    "Spark": r"\bspark\b",
    "AWS": r"\baws\b",
    "Azure": r"\bazure\b",
    "GCP": r"\bgcp\b|\bgoogle cloud\b",
    "Docker": r"\bdocker\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "Terraform": r"\bterraform\b",
    "Linux": r"\blinux\b",
    "PostgreSQL": r"\bpostgres(?:ql)?\b",
    "MongoDB": r"\bmongodb\b",
    "Redis": r"\bredis\b",
    "GraphQL": r"\bgraphql\b",
    "REST": r"\brest(?:ful)?\b",
    "gRPC": r"\bgrpc\b",
    "OOP": r"\boop\b|\bobject[-\s]oriented\b",
    "Machine Learning": r"\bmachine learning\b|\bml\b",
    "Deep Learning": r"\bdeep learning\b",
    "NLP": r"\bnlp\b|\bnatural language processing\b",
    "Computer Vision": r"\bcomputer vision\b",
    "Distributed Systems": r"\bdistributed systems?\b",
    "Microservices": r"\bmicroservices?\b",
    "CI/CD": r"\bci/cd\b",
    "Agile": r"\bagile\b",
    "Scrum": r"\bscrum\b",
    "Algorithms": r"\balgorithms?\b",
    "Data Structures": r"\bdata structures?\b",
}
_SKILL_LIMIT = 10


def _extract_keywords(description: str) -> list[str]:
    # Order by first appearance so the most prominent skills lead.
    found: list[tuple[int, str]] = []
    for canonical, pattern in _SKILLS.items():
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            found.append((match.start(), canonical))
    found.sort()
    return [name for _, name in found[:_SKILL_LIMIT]]
