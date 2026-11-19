"""The Job schema - the one shape the entire pipeline speaks.

Every adapter returns a list of these; every later stage (dedup, filter,
notify) consumes them without knowing which source they came from.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    id: str  # stable dedup key: "{source_type}:{company}:{ats_job_id}",
    #          falling back to a hash of the URL if no ATS id exists
    title: str
    company: str
    location: str
    url: str  # the apply / posting link
    posted_at: str | None
    description: str  # plain text, truncated to ~500 chars
    source: str  # human-readable, e.g. "ashby/wealthsimple"


class PartialFetch(list):
    """The jobs an adapter got before it had to stop early - throttled on a
    later page, or out of run time. It is still a ``list[Job]`` to everything
    downstream; only ``main.fetch_all`` looks closer, to treat it as the
    board's newest slice rather than the whole board (closure tracking)."""

    def __init__(self, jobs: list[Job], throttled: bool = False) -> None:
        super().__init__(jobs)
        self.throttled = throttled  # stopped by a 429, not by the clock


@dataclass(frozen=True)
class JobNotes:
    """What past runs know about a job, for notify to show next to it.
    Computed from state (scraper.insights), never by adapters."""

    reposted_since: str | None = None  # date the same role was first seen
    typical_open_days: int | None = None  # median lifetime of this source's postings
