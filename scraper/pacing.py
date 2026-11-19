"""Pacing for providers that throttle us.

An adapter sees one board at a time, but throttling is about the run as a
whole: how many of a provider's boards it touches, in what order, how fast,
and when to give up for now. Those are decisions across sources, so they
live here rather than in the adapter. The orchestrator still never names a
source type - it asks each adapter for its ``PACING`` policy, the same way
it asks for MAX_POSTINGS - so any provider can opt in. Workday is the one
that does (see ``adapters/workday.py`` for why).

A policy holds:

- ``every_minutes``: a board is due again this long after its last attempt.
- ``per_run``: at most this many due boards per run, longest-waiting first,
  so the provider's boards rotate across runs instead of all being hit at
  once (and a board that was never attempted goes first).
- ``budget_seconds``: the provider's share of a run's time. Once spent, the
  remaining due boards wait; the adapter gets the deadline as
  ``config["_deadline"]`` so a backoff never sleeps past it.
- ``pause_seconds``: a random pause between two boards of the provider.
- ``breaker_after``: this many boards in a row came back throttled (429)
  -> leave the provider alone for the rest of the run.

A board skipped for any of these reasons is left out of this run entirely
(``"skipped"`` in its stats): no health counting, no closure tracking. It
is not stamped as attempted either, so it stays first in line next run.

Last-attempt times live in ``store.polling`` and are saved with the rest of
the run's state; the poll workflow's concurrency group means only one run
ever writes them.
"""

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from scraper import autosources
from scraper.adapters import get_adapter, pacing
from scraper.store import SeenStore

log = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=UTC)


class Pacer:
    def __init__(
        self,
        sources: list[dict],
        store: SeenStore,
        now: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.now = now or datetime.now(UTC)
        self.polled = store.polling
        self.clock = clock
        self.sleep = sleep
        self.due: set[str] = set()
        self._runs: dict[str, dict] = {}  # per source type: deadline, throttled streak
        self._select(sources)

    def _select(self, sources: list[dict]) -> None:
        candidates: dict[str, list[tuple[datetime, int, str]]] = {}
        paced_labels = set()
        for index, source in enumerate(sources):
            policy = _policy(source)
            if not policy:
                continue
            label = autosources.label(source)
            paced_labels.add(label)
            last = _parse(self.polled.get(label))
            if last and self.now - last < timedelta(minutes=policy["every_minutes"]):
                continue
            candidates.setdefault(source["type"], []).append((last or _EPOCH, index, label))
        for type_str, boards in candidates.items():
            per_run = _policy({"type": type_str})["per_run"]
            self.due.update(label for _, _, label in sorted(boards)[:per_run])
        for label in [label for label in self.polled if label not in paced_labels]:
            del self.polled[label]  # a board no longer polled

    def admit(self, source: dict, label: str) -> bool:
        """Whether to fetch this source now. Pauses before a provider's
        second and later boards, so call it right before fetching."""
        policy = _policy(source)
        if not policy:
            return True
        if label not in self.due:
            return False
        run = self._runs.setdefault(source["type"], {"deadline": None, "streak": 0})
        if run.get("stopped"):
            return False
        if run["streak"] >= policy["breaker_after"]:
            log.warning("%s: %d boards in a row throttled; leaving the rest for the next run.",
                        source["type"], run["streak"])
            run["stopped"] = True
            return False
        if run["deadline"] is None:
            run["deadline"] = self.clock() + policy["budget_seconds"]
            return True
        if self.clock() >= run["deadline"]:
            log.info("%s: this run's %ds budget is spent; the remaining due boards go next run.",
                     source["type"], policy["budget_seconds"])
            run["stopped"] = True
            return False
        self.sleep(random.uniform(*policy["pause_seconds"]))
        return True

    def config(self, source: dict) -> dict:
        """The source config to fetch with: plus the run's deadline if paced."""
        run = self._runs.get(source.get("type"))
        if not _policy(source) or not run:
            return source
        return {**source, "_deadline": run["deadline"]}

    def done(self, source: dict, label: str, throttled: bool) -> None:
        """Record an attempt - successful, partial or failed."""
        if not _policy(source):
            return
        self.polled[label] = self.now.isoformat(timespec="seconds")
        run = self._runs[source["type"]]
        run["streak"] = run["streak"] + 1 if throttled else 0


def _policy(source: dict) -> dict | None:
    try:
        return pacing(get_adapter(source["type"]))
    except KeyError:
        return None  # an unknown type fails in fetch_all, with its own message


def _parse(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None
