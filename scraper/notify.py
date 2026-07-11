"""Telegram sender.

Posts one message per job via the Bot API sendMessage endpoint (HTML parse
mode). Credentials come only from environment variables, injected by GitHub
Actions Secrets - never from config files.
"""

import html
import logging
import os
import re
import time
from datetime import datetime

import requests

from scraper.models import Job

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_PAUSE_SECONDS = 0.5  # stay well under Telegram's rate limits
TIMEOUT_SECONDS = 30

# Seniority buckets, in the order their digest messages are sent.
CATEGORIES = (
    ("internship", "🌱", "INTERNSHIPS"),
    ("new_grad", "🎓", "NEW GRAD & JUNIOR"),
    ("full_time", "💼", "FULL-TIME"),
)
_EMOJI = {key: emoji for key, emoji, _ in CATEGORIES}

_INTERN_RE = re.compile(r"\bintern(ship)?\b|\bco[-\s]?op\b", re.IGNORECASE)
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
    if _NEW_GRAD_RE.search(job.title):
        return "new_grad"
    return "full_time"


def send(job: Job) -> None:
    _post(format_message(job))
    log.info("Notified: %s", job.id)
    time.sleep(SEND_PAUSE_SECONDS)


def send_digest(jobs: list[Job]) -> None:
    messages = format_digest(jobs)
    for message in messages:
        _post(message)
        time.sleep(SEND_PAUSE_SECONDS)
    log.info("Notified: digest of %d jobs in %d message(s)", len(jobs), len(messages))


def send_text(text: str) -> None:
    """Send a plain (non-job) message, e.g. a health warning."""
    _post(html.escape(text))
    log.info("Notified: %s", text)


def _post(text: str) -> None:
    # Strip whitespace: a token pasted into GitHub Secrets with a trailing
    # newline becomes %0A in the URL and Telegram answers 404.
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    response = requests.post(
        API_URL.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def format_message(job: Job) -> str:
    # Telegram HTML mode breaks on unescaped <, >, & - escape everything
    # that originates from the source.
    e = html.escape
    lines = [
        f"<b>=== {_EMOJI[categorize(job)]} {e(job.title)} ===</b>",
        f"{e(job.company)} - {e(job.location)}",
    ]
    if job.posted_at:
        lines.append(f"Posted: {e(_date_only(job.posted_at))}")
    if job.description:
        lines.append("")
        lines.append(e(job.description))
    lines.append("")
    lines.append(f'<a href="{e(job.url, quote=True)}">Apply</a> ({e(job.source)})')
    return "\n".join(lines)


def format_digest(jobs: list[Job]) -> list[str]:
    # One message (or more, if long) per seniority bucket, so internships,
    # new-grad roles, and full-time roles never share a message.
    messages: list[str] = []
    for key, emoji, name in CATEGORIES:
        group = [job for job in jobs if categorize(job) == key]
        if group:
            messages.extend(_format_group(f"{emoji} {name}", group))
    return messages


def _format_group(label: str, jobs: list[Job]) -> list[str]:
    # Split across as many messages as needed; each stays safely under
    # Telegram's 4096-char cap so no job is ever dropped.
    e = html.escape
    header = f"<b>==== {label} ({len(jobs)}) ====</b>"
    messages: list[str] = []
    lines = [header, ""]
    budget = 3800 - sum(len(line) + 1 for line in lines)
    for job in jobs:
        line = f'- <a href="{e(job.url, quote=True)}">{e(job.title)}</a> - {e(job.company)}'
        if len(line) + 1 > budget and len(lines) > 2:
            messages.append("\n".join(lines))
            lines = [f"{header} (continued)", ""]
            budget = 3800 - sum(len(line) + 1 for line in lines)
        lines.append(line)
        budget -= len(line) + 1
    messages.append("\n".join(lines))
    return messages


def _date_only(posted_at: str) -> str:
    try:
        return datetime.fromisoformat(posted_at).date().isoformat()
    except ValueError:
        return posted_at
