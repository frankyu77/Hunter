"""Find company boards worth polling directly.

Aggregator feeds (SimplifyJobs and co.) list a job only once someone adds
it - hours to days after the company posted it. Their apply links, though,
usually point straight at the company's own job board, which Hunter could
poll itself. This module mines those links for boards not yet in
sources.yaml, tallies how many relevant jobs each would have delivered
sooner, and once a week suggests the top few as ready-to-paste entries.

Every suggestion is verified first by fetching it with the real adapter:
a board that errors or lists nothing (the "migration trap" in
sources.yaml) is never suggested.

Links are recognised by host for hosted boards (Greenhouse, Lever, Ashby,
Workday, SmartRecruiters, ...), and by a telltale query parameter for
boards behind a company's own domain: ``?icims=1`` (iCIMS Jibe sites),
``?ats=successfactors``, and ``?gh_jid=`` (a Greenhouse board embedded on
the company site). The last carries only a job id, so its board slug is
resolved at report time from Greenhouse's embed redirect - the name can't be
guessed (Optiver's board is "optiverus").

Some values aren't in any link and are guessed, with the probe catching a
wrong guess: Oracle's API site number ("CX_1" unless the site name already
looks like one) and Eightfold's company domain ("{tenant}.com").

Links on platforms with no adapter are tallied too, so the report can say
which adapter would be worth writing next.
"""

import html
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import requests
import yaml

from scraper.adapters import get_adapter, max_postings
from scraper.models import Job
from scraper.notify import display_company
from scraper.store import SeenStore

log = logging.getLogger(__name__)

REPORT_EVERY_DAYS = 7
SUGGESTIONS = 5
GAPS = 3  # unsupported platforms named per report
MIN_MATCHED = 2  # one matching job is a coincidence, not a pattern
MAX_PROBES = 10  # verification requests per report, however many fail
GAP_COMPANY_SAMPLE = 20
TIMEOUT_SECONDS = 30

_AGGREGATOR_PREFIX = "github/"
_GREENHOUSE_HOSTS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
}
_GREENHOUSE_EMBED = "https://boards.greenhouse.io/embed/job_app"
_LOCALE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.IGNORECASE)
_WORKDAY_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_WORKDAY_SITE_HOST = re.compile(r"^(wd\d+)\.myworkdaysite\.com$")
_ORACLE_HOST = re.compile(r"^[a-z0-9]+\.fa(?:\.[a-z0-9]+)?\.oraclecloud\.com$")
_EIGHTFOLD_HOST = re.compile(r"^([a-z0-9-]+)\.eightfold\.ai$")
# Eightfold sites on a company's own domain: host -> (tenant, email domain).
_EIGHTFOLD_CUSTOM_HOSTS = {"apply.careers.microsoft.com": ("microsoft", "microsoft.com")}
_CX_SITE = re.compile(r"^CX_\d+$")
# One employer, one board: keyed by type alone.
_SINGLE_BOARD = {"tiktok", "amazon"}
# Region filter names -> each adapter's country codes.
_COUNTRY_CODES = {
    "smartrecruiters": {"us": "us", "canada": "ca"},
    "amazon": {"us": "USA", "canada": "CAN"},
}
# Unsupported platforms worth naming as a whole rather than host by host.
_PLATFORMS = [
    (re.compile(r"(^|\.)icims\.com$"), "iCIMS portals"),
    (re.compile(r"(^|\.)applytojob\.com$"), "JazzHR"),
    (re.compile(r"(^|\.)taleo\.net$"), "Taleo"),
    (re.compile(r"(^|\.)paylocity\.com$"), "Paylocity"),
    (re.compile(r"(^|\.)jobvite\.com$"), "Jobvite"),
    (re.compile(r"(^|\.)avature\.net$"), "Avature"),
    (re.compile(r"(^|\.)ultipro\.com$|(^|\.)ukg\.net$"), "UKG"),
    (re.compile(r"(^|\.)adp\.com$"), "ADP"),
    (re.compile(r"(^|\.)dayforcehcm\.com$"), "Dayforce"),
    (re.compile(r"(^|\.)brassring\.com$"), "BrassRing"),
]


def board_for(job: Job) -> dict | None:
    """The sources.yaml entry that would poll ``job``'s board directly, or
    None when its apply link isn't on a board type Hunter supports."""
    url = urlparse(job.url)
    host = url.netloc.lower()
    parts = [unquote(part) for part in url.path.split("/") if part]
    query = parse_qs(url.query)
    slug = _slugify(job.company)

    if host in _GREENHOUSE_HOSTS:
        company = query.get("for", [None])[0] if parts[:1] == ["embed"] else _first(parts)
        return {"type": "greenhouse", "company": company} if company else None
    if host == "jobs.ashbyhq.com" and parts:
        return {"type": "ashby", "company": parts[0]}
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        board = {"type": "lever", "company": parts[0]}
        return board | {"region": "eu"} if host.startswith("jobs.eu.") else board
    if match := _WORKDAY_HOST.match(host):
        site = _first([p for p in parts if not _LOCALE.match(p)])
        return site and {"type": "workday", "company": slug or match.group(1),
                         "tenant": match.group(1), "host": match.group(2), "site": site}
    if (match := _WORKDAY_SITE_HOST.match(host)) and "recruiting" in parts:
        rest = parts[parts.index("recruiting") + 1:]
        if len(rest) >= 2:
            return {"type": "workday", "company": slug or rest[0], "tenant": rest[0],
                    "host": match.group(1), "site": rest[1], "domain": "myworkdaysite.com"}
        return None
    if _ORACLE_HOST.match(host) and "sites" in parts[:-1]:
        site_name = parts[parts.index("sites") + 1]
        site_number = site_name if _CX_SITE.match(site_name) else "CX_1"
        return {"type": "oracle", "company": slug, "host": host, "site_number": site_number,
                "site_name": site_name}
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        return {"type": "smartrecruiters", "company": parts[0]}
    if host == "apply.workable.com" and parts and parts[0] != "api":
        return {"type": "workable", "company": parts[0]}
    if host == "ats.rippling.com" and parts:
        return {"type": "rippling", "company": parts[0]}
    if host.endswith(".bamboohr.com"):
        return {"type": "bamboohr", "company": host.removesuffix(".bamboohr.com")}
    if host in _EIGHTFOLD_CUSTOM_HOSTS:
        tenant, domain = _EIGHTFOLD_CUSTOM_HOSTS[host]
        return {"type": "eightfold", "company": tenant, "tenant": tenant, "host": host,
                "domain": domain}
    if match := _EIGHTFOLD_HOST.match(host):
        tenant = match.group(1)
        return {"type": "eightfold", "company": slug or tenant, "tenant": tenant,
                "domain": f"{tenant}.com"}
    if host in ("lifeattiktok.com", "www.lifeattiktok.com", "careers.tiktok.com"):
        # Early careers only: the whole ~4.3k board overflows the adapter's cap.
        return {"type": "tiktok", "recruitment_ids": ["2"]}
    if host in ("amazon.jobs", "www.amazon.jobs"):
        return {"type": "amazon"}
    if "icims" in query and not host.endswith("icims.com"):
        return {"type": "jibe", "company": slug, "host": host}
    if query.get("ats") == ["successfactors"]:
        return {"type": "successfactors", "company": slug, "host": host}
    if gh_jid := query.get("gh_jid", [None])[0]:
        return {"type": "greenhouse", "gh_jid": gh_jid, "via": host}
    return None


def board_key(source: dict) -> str:
    """Identity of a board, comparable between sources.yaml entries and
    discovered ones - never the free-form company label where a board has a
    real identifier. A Workday tenant often runs several career sites (HPE:
    "Jobsathpe" and "acjobsite"); one polled site means the company is
    already watched, so the site is left out of the key. An unresolved
    embedded-Greenhouse board is keyed by the site it was seen on."""
    kind = source.get("type", "")
    if kind in _SINGLE_BOARD:
        return kind
    if kind == "greenhouse" and "gh_jid" in source:
        return f"greenhouse/@{source.get('via')}".lower()
    field = {"workday": "tenant", "oracle": "host", "jibe": "host", "successfactors": "host",
             "eightfold": "tenant"}.get(kind, "company")
    return f"{kind}/{source.get(field)}".lower()


def platform_of(job: Job) -> str:
    host = urlparse(job.url).netloc.lower()
    for pattern, name in _PLATFORMS:
        if pattern.search(host):
            return name
    return host.removeprefix("www.") or "(no link)"


def _first(parts: list[str]) -> str | None:
    return parts[0] if parts else None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _section(store: SeenStore) -> dict:
    return store.insights.setdefault("discovery", {})


def observe(
    fresh: list[Job], matched: list[Job], store: SeenStore, config: dict,
    now: datetime | None = None,
) -> None:
    """Tally this run's new aggregator jobs by the direct board behind them,
    or by platform when Hunter has no adapter for it."""
    section = _section(store)
    section.setdefault("since", (now or datetime.now(UTC)).isoformat(timespec="seconds"))
    skip = _configured(config) | _ignored(config)
    matched_ids = {job.id for job in matched}
    candidates = section.setdefault("candidates", {})
    gaps = section.setdefault("gaps", {})
    for job in fresh:
        if not job.source.startswith(_AGGREGATOR_PREFIX):
            continue
        is_match = job.id in matched_ids
        board = board_for(job)
        if board is None:
            gap = gaps.setdefault(platform_of(job), {"jobs": 0, "matched": 0, "companies": []})
            gap["jobs"] += 1
            gap["matched"] += is_match
            name = display_company(job.company)
            if name not in gap["companies"] and len(gap["companies"]) < GAP_COMPANY_SAMPLE:
                gap["companies"].append(name)
            continue
        if (key := board_key(board)) in skip:
            continue
        entry = candidates.setdefault(
            key, {"boards": {}, "name": display_company(job.company), "jobs": 0, "matched": 0}
        )
        entry["jobs"] += 1
        entry["matched"] += is_match
        # A tenant's jobs can link to several career sites; count each shape
        # so the suggestion uses the one most of them came through. Any one
        # job id resolves an embedded-Greenhouse board, so keep the first.
        variant = json.dumps(board, sort_keys=True)
        if "gh_jid" in board:
            variant = next(iter(entry["boards"]), variant)
        entry["boards"][variant] = entry["boards"].get(variant, 0) + 1


def _configured(config: dict) -> set[str]:
    return {board_key(source) for source in config.get("sources") or []}


def _ignored(config: dict) -> set[str]:
    ignore = (config.get("discovery") or {}).get("ignore") or []
    return {str(key).lower() for key in ignore}


def due_report(
    store: SeenStore, config: dict, now: datetime | None = None,
    probe: Callable[[dict], int | None] | None = None,
    resolve: Callable[[str], str | None] | None = None,
) -> str | None:
    """The weekly suggestion message, or None if it isn't due or nothing
    qualified. The caller resets the tally once the message is out (or when
    nothing qualified), so a failed send is retried on the next run."""
    now = now or datetime.now(UTC)
    if not is_due(store, now):
        return None
    section = _section(store)
    since = datetime.fromisoformat(section["since"])
    probe = probe or _probe
    resolve = resolve or resolve_greenhouse_board

    # sources.yaml may have changed since these were tallied.
    skip = _configured(config) | _ignored(config)
    ranked = sorted(
        (
            (key, entry) for key, entry in section.get("candidates", {}).items()
            if key not in skip and entry["matched"] >= MIN_MATCHED
        ),
        key=lambda item: (item[1]["matched"], item[1]["jobs"]),
        reverse=True,
    )
    picks = []
    for key, entry in ranked[:MAX_PROBES]:
        if len(picks) == SUGGESTIONS:
            break
        board = _likeliest_board(entry, config)
        if "gh_jid" in board:
            slug = resolve(board["gh_jid"])
            board = {"type": "greenhouse", "company": slug} if slug else None
            if board is None or board_key(board) in skip:
                continue
            key = board_key(board)
        open_now = probe(board)
        if open_now:
            picks.append((key, entry | {"board": board}, open_now))
        else:
            log.info("Discovery: %s did not verify; not suggesting it.", key)

    gaps = sorted(
        ((name, gap) for name, gap in section.get("gaps", {}).items()
         if gap["matched"] >= MIN_MATCHED),
        key=lambda item: (item[1]["matched"], item[1]["jobs"]),
        reverse=True,
    )[:GAPS]
    if not picks and not gaps:
        return None
    return format_report(picks, gaps, (now - since).days)


def _likeliest_board(entry: dict, config: dict) -> dict:
    variant = max(entry["boards"], key=entry["boards"].get)
    board = json.loads(variant)
    order = ["type", "company", "tenant", "host", "site", "domain", "site_number",
             "site_name", "region", "recruitment_ids", "gh_jid", "via"]
    board = {key: board[key] for key in order if key in board}
    # Narrow boards whose adapter filters by country to the regions you keep.
    codes = _COUNTRY_CODES.get(board["type"], {})
    regions = (config.get("filters") or {}).get("regions") or []
    countries = [codes[r] for r in regions if r in codes]
    if countries:
        board["countries"] = countries
    return board


def resolve_greenhouse_board(gh_jid: str) -> str | None:
    """The board slug a Greenhouse job id belongs to. Greenhouse's embed page
    redirects ``?token=<id>`` to ``?for=<slug>&token=<id>``."""
    try:
        response = requests.get(
            _GREENHOUSE_EMBED, params={"token": gh_jid}, allow_redirects=False,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        log.info("Discovery: resolving gh_jid %s failed: %s", gh_jid, exc)
        return None
    location = parse_qs(urlparse(response.headers.get("Location", "")).query)
    slug = location.get("for", [""])[0]
    return slug or None


def is_due(store: SeenStore, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    since = _section(store).setdefault("since", now.isoformat(timespec="seconds"))
    return now - datetime.fromisoformat(since) >= timedelta(days=REPORT_EVERY_DAYS)


def reset(store: SeenStore, now: datetime | None = None) -> None:
    section = _section(store)
    section["since"] = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    section["candidates"] = {}
    section["gaps"] = {}


def _probe(board: dict) -> int | None:
    """Postings the board lists right now; None if it can't be fetched.
    A capped count comes back negative, meaning "at least this many"."""
    try:
        fetch = get_adapter(board["type"])
        count = len(fetch(board))
    except Exception as exc:
        log.info("Discovery probe of %s failed: %s", board_key(board), exc)
        return None
    cap = max_postings(fetch)
    return -count if cap is not None and count >= cap else count


def format_report(
    picks: list[tuple[str, dict, int]], gaps: list[tuple[str, dict]], days: int
) -> str:
    e = html.escape
    lines = ["🧭 <b>Worth polling directly?</b>"]
    if picks:
        lines.append(
            f"In the last {days} days these companies' jobs reached you only through "
            "aggregator feeds, hours to days after posting. Add them to "
            "<code>sources.yaml</code> to catch new postings within minutes."
        )
    for number, (key, entry, open_now) in enumerate(picks, 1):
        board_size = f"{-open_now}+" if open_now < 0 else str(open_now)
        lines += [
            "",
            f"<b>{number}. {e(entry['name'])}</b> — {entry['matched']} matching of "
            f"{entry['jobs']} new jobs · {board_size} open now",
            f"<pre>{e(_yaml_entry(entry['board']))}</pre>",
            f"ignore key: <code>{e(key)}</code>",
        ]
    if picks:
        lines += [
            "",
            "Not interested in one? Add its ignore key under "
            "<code>discovery: ignore:</code> in sources.yaml.",
        ]
    if gaps:
        lines += ["", "<b>No adapter yet</b> — where the rest of your matches came from:"]
        for name, gap in gaps:
            sample = ", ".join(gap["companies"][:3])
            more = len(gap["companies"]) - 3
            companies = f"{sample} +{more} more" if more > 0 else sample
            lines.append(
                f"• {e(name)} — {gap['matched']} matching of {gap['jobs']} jobs "
                f"({e(companies)})"
            )
    return "\n".join(lines)


def _yaml_entry(board: dict) -> str:
    """Indented to paste straight under ``sources:``."""
    dumped = yaml.safe_dump([board], sort_keys=False, default_flow_style=False)
    return "\n".join(f"  {line}" for line in dumped.splitlines())
