"""Telegram sender.

Posts one message per job via the Bot API sendMessage endpoint (HTML parse
mode). Credentials come only from environment variables, injected by GitHub
Actions Secrets - never from config files.
"""

import html
import logging
import os
import time
from datetime import datetime

import requests

from scraper.models import Job

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_PAUSE_SECONDS = 0.5  # stay well under Telegram's rate limits
TIMEOUT_SECONDS = 30


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
    lines = [f"<b>{e(job.title)}</b>", f"{e(job.company)} - {e(job.location)}"]
    if job.posted_at:
        lines.append(f"Posted: {e(_date_only(job.posted_at))}")
    if job.description:
        lines.append("")
        lines.append(e(job.description))
    lines.append("")
    lines.append(f'<a href="{e(job.url, quote=True)}">Apply</a> ({e(job.source)})')
    return "\n".join(lines)


def format_digest(jobs: list[Job]) -> list[str]:
    # Split across as many messages as needed; each stays safely under
    # Telegram's 4096-char cap so no job is ever dropped.
    e = html.escape
    header = f"<b>{len(jobs)} new matching jobs this run</b>"
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
