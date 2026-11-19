"""Per-source health tracking: surface silent breakage.

Counters live in the "health" section of seen_jobs.json (via
``SeenStore.health``) and catch two failure modes:

- a source that keeps erroring (5 consecutive failed runs), and
- a source that keeps "succeeding" with zero jobs for 7 days - the
  signature of a quiet ATS migration, where an abandoned board returns
  200 with an empty list forever.

A source that is only being rate-limited (HTTP 429) is not broken: the
provider is busy, or pacing it, and an intermittent 429 says nothing about
the config. Its streak still counts, but it warns only once it has gone
THROTTLED_HOURS_THRESHOLD hours without a single successful fetch. A board
the pacer skipped this run (``"skipped"`` in its stats) wasn't fetched, so
its counters are left exactly as they were.

Warnings are one-time: a per-condition "warned" flag is set when the
threshold is crossed and reset when the source recovers, so a broken
source produces exactly one Telegram warning, not one per run.
"""

from datetime import UTC, date, datetime, timedelta

CONSECUTIVE_FAILURES_THRESHOLD = 5
THROTTLED_HOURS_THRESHOLD = 24
ZERO_JOBS_DAYS_THRESHOLD = 7


def record_run(
    health: dict, stats: dict, today: date | None = None, now: datetime | None = None
) -> list[str]:
    """Update per-source counters from one run's stats; return the warning
    messages (if any) that should be sent this run.

    ``stats`` maps a source label to {"fetched": int, "errors": int}, plus
    optional "throttled" / "skipped" flags. ``today`` and ``now`` are
    injectable for tests.
    """
    now = now or datetime.now(UTC)
    today = today or now.date()
    warnings = []

    # Drop counters for sources no longer in the config.
    for label in [label for label in health if label not in stats]:
        del health[label]

    for label, stat in stats.items():
        if stat.get("skipped"):
            continue
        entry = health.setdefault(
            label,
            {
                "consecutive_failures": 0,
                "zero_since": None,
                "warned_failures": False,
                "warned_zero": False,
            },
        )

        if stat["errors"]:
            # Set on the first failure after a success; an entry already
            # failing when this field was introduced counts from now.
            entry.setdefault("failing_since", now.isoformat(timespec="seconds"))
            entry["consecutive_failures"] += 1
            if stat.get("error"):
                entry["last_error"] = stat["error"]
            if stat.get("throttled"):
                entry["throttled"] = True
            else:
                entry.pop("throttled", None)
            if _should_warn_failures(entry, now):
                entry["warned_failures"] = True
                last = f" Last error: {entry['last_error']}" if entry.get("last_error") else ""
                if entry.get("throttled"):
                    warnings.append(
                        f"{label} has been rate-limited with no successful fetch since "
                        f"{entry['failing_since']} ({entry['consecutive_failures']} attempts)."
                        f"{last}"
                    )
                else:
                    warnings.append(
                        f"{label} has failed {entry['consecutive_failures']} runs in a row."
                        f"{last} Check the source config or the Actions logs."
                    )
            continue

        entry["consecutive_failures"] = 0
        entry["warned_failures"] = False
        for key in ("last_error", "failing_since", "throttled"):
            entry.pop(key, None)

        if stat["fetched"] == 0:
            if entry["zero_since"] is None:
                entry["zero_since"] = today.isoformat()
            days = (today - date.fromisoformat(entry["zero_since"])).days
            if days >= ZERO_JOBS_DAYS_THRESHOLD and not entry["warned_zero"]:
                entry["warned_zero"] = True
                warnings.append(
                    f"{label} has returned zero jobs since {entry['zero_since']} "
                    f"({days} days). The company may have migrated to a different ATS - "
                    "check where its Apply links point."
                )
        else:
            entry["zero_since"] = None
            entry["warned_zero"] = False

    return warnings


def _should_warn_failures(entry: dict, now: datetime) -> bool:
    if entry["warned_failures"]:
        return False
    if entry.get("throttled"):
        since = datetime.fromisoformat(entry["failing_since"])
        return now - since >= timedelta(hours=THROTTLED_HOURS_THRESHOLD)
    return entry["consecutive_failures"] >= CONSECUTIVE_FAILURES_THRESHOLD
