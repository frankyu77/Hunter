"""Adapter registry: maps a sources.yaml ``type:`` string to a fetch function.

Every adapter module exposes ``fetch(config: dict) -> list[Job]`` and is
registered here. Adding a source type = one new adapter file + one entry in
REGISTRY; the orchestrator dispatches purely by type string.
"""

import inspect
from collections.abc import Callable

from scraper.adapters import (
    amazon,
    ashby,
    bamboohr,
    eightfold,
    github_repo,
    greenhouse,
    jibe,
    lever,
    oracle,
    rippling,
    smartrecruiters,
    successfactors,
    tiktok,
    workable,
    workday,
)
from scraper.models import Job

REGISTRY: dict[str, Callable[[dict], list[Job]]] = {
    "ashby": ashby.fetch,
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "github": github_repo.fetch,
    "workday": workday.fetch,
    "oracle": oracle.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "workable": workable.fetch,
    "rippling": rippling.fetch,
    "bamboohr": bamboohr.fetch,
    "jibe": jibe.fetch,
    "successfactors": successfactors.fetch,
    "eightfold": eightfold.fetch,
    "tiktok": tiktok.fetch,
    "amazon": amazon.fetch,
}


def max_postings(fetch: Callable[[dict], list[Job]]) -> int | None:
    """The adapter's MAX_POSTINGS cap, if it has one. A fetch returning that
    many jobs is only the newest slice of the board, not all of it."""
    return getattr(inspect.getmodule(fetch), "MAX_POSTINGS", None)


def newest_first(fetch: Callable[[dict], list[Job]]) -> bool:
    """Whether the adapter's results come newest-first. Capped adapters that
    say False (NEWEST_FIRST = False) return an arbitrary slice when they hit
    the cap, which closure tracking must not read as a recency window."""
    return getattr(inspect.getmodule(fetch), "NEWEST_FIRST", True)


def get_adapter(type_str: str) -> Callable[[dict], list[Job]]:
    try:
        return REGISTRY[type_str]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none registered yet)"
        raise KeyError(
            f"Unknown source type {type_str!r} in sources.yaml. Known types: {known}"
        ) from None
