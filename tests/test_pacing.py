"""Pacing throttled providers: interval, rotation, budget and circuit breaker."""

from datetime import UTC, datetime, timedelta

import pytest
import requests

from scraper import health, main, pacing
from scraper.adapters import REGISTRY, workday
from scraper.models import Job
from scraper.store import SeenStore

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
POLICY = {"every_minutes": 20, "per_run": 3, "budget_seconds": 90,
          "pause_seconds": (1, 3), "breaker_after": 2}


def board(name: str) -> dict:
    return {"type": "workday", "company": name, "tenant": name, "host": "wd1", "site": "x"}


BOARDS = [board(f"b{n}") for n in range(7)]


def job(source: str) -> Job:
    return Job(id=f"{source}:1", title="Engineer", company="x", location="Remote",
               url="https://example.com/1", posted_at=None, description="", source=source)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)


@pytest.fixture
def adapter(monkeypatch):
    """A fake Workday fetch: outcomes[label] is "ok", "throttled" or a callable."""
    outcomes: dict[str, object] = {}
    calls: list[str] = []

    def fetch(config):
        label = f"workday/{config['company']}"
        calls.append(label)
        outcome = outcomes.get(label, "ok")
        if callable(outcome):
            return outcome(config)
        if outcome == "throttled":
            response = requests.Response()
            response.status_code = 429
            response.url = f"https://{config['tenant']}.wd1.myworkdayjobs.com/x"
            error = requests.exceptions.HTTPError(response=response)
            error.already_retried = True
            raise error
        return [job(label)]

    fetch.__module__ = workday.__name__  # so pacing() finds the module's PACING
    monkeypatch.setattr(workday, "PACING", POLICY)
    monkeypatch.setitem(REGISTRY, "workday", fetch)
    return outcomes, calls


def pacer(store, now=NOW, clock=None, sources=BOARDS):
    clock = clock or Clock()
    return pacing.Pacer(sources, store, now, clock=clock, sleep=clock.sleep)


def run(store, now=NOW, clock=None, sources=BOARDS):
    return main.fetch_all(sources, pacer(store, now, clock, sources))


def test_a_run_fetches_only_a_few_boards_and_rotates_through_the_rest(tmp_path, adapter):
    _, calls = adapter
    store = SeenStore(str(tmp_path / "seen.json"))

    _, stats = run(store)
    assert calls == ["workday/b0", "workday/b1", "workday/b2"]  # per_run, config order at first
    assert stats["workday/b5"] == {"fetched": 0, "errors": 0, "skipped": True}

    calls.clear()
    run(store, NOW + timedelta(minutes=5))
    assert calls == ["workday/b3", "workday/b4", "workday/b5"]  # never-attempted boards next

    calls.clear()
    run(store, NOW + timedelta(minutes=10))
    assert calls == ["workday/b6"]  # b0-b2 aren't due again until 20 minutes on

    calls.clear()
    run(store, NOW + timedelta(minutes=20))
    assert calls == ["workday/b0", "workday/b1", "workday/b2"]  # the longest-waiting first


def test_boards_of_other_types_are_never_paced(tmp_path, adapter, monkeypatch):
    monkeypatch.setitem(REGISTRY, "plain", lambda config: [job("plain/a")])
    store = SeenStore(str(tmp_path / "seen.json"))
    sources = [{"type": "plain", "company": "a"}]
    for minute in (0, 1, 2):
        jobs, _ = run(store, NOW + timedelta(minutes=minute), sources=sources)
        assert len(jobs) == 1
    assert store.polling == {}


def test_pauses_between_boards_and_stops_when_the_budget_is_spent(tmp_path, adapter):
    outcomes, calls = adapter
    clock = Clock()

    def slow(config):
        clock.t += 50
        return [job(f"workday/{config['company']}")]

    for name in ("b0", "b1", "b2"):
        outcomes[f"workday/{name}"] = slow
    store = SeenStore(str(tmp_path / "seen.json"))

    _, stats = run(store, clock=clock)

    assert calls == ["workday/b0", "workday/b1"]  # 100 s spent of 90: b2 waits
    assert stats["workday/b2"]["skipped"]
    assert len(clock.pauses) == 1 and 1 <= clock.pauses[0] <= 3  # only *between* boards
    assert "workday/b2" not in store.polling  # not attempted, so first in line next run


def test_the_adapter_gets_the_run_deadline(tmp_path, adapter):
    outcomes, _ = adapter
    seen = []
    outcomes["workday/b0"] = lambda config: seen.append(config["_deadline"]) or []
    run(SeenStore(str(tmp_path / "seen.json")), sources=BOARDS[:1])
    assert seen == [POLICY["budget_seconds"]]  # clock starts at 0


def test_breaker_leaves_workday_alone_after_boards_in_a_row_are_throttled(tmp_path, adapter):
    outcomes, calls = adapter
    outcomes["workday/b0"] = outcomes["workday/b1"] = "throttled"
    store = SeenStore(str(tmp_path / "seen.json"))

    _, stats = run(store)

    assert calls == ["workday/b0", "workday/b1"]
    assert stats["workday/b0"]["throttled"] and stats["workday/b0"]["errors"] == 1
    assert stats["workday/b2"]["skipped"]
    assert set(store.polling) == {"workday/b0", "workday/b1"}  # b2 goes first next run

    calls.clear()
    run(store, NOW + timedelta(minutes=5))
    assert calls[0] == "workday/b2"


def test_a_success_in_between_resets_the_breaker(tmp_path, adapter):
    outcomes, calls = adapter
    outcomes["workday/b0"] = outcomes["workday/b2"] = "throttled"
    run(SeenStore(str(tmp_path / "seen.json")))
    assert calls == ["workday/b0", "workday/b1", "workday/b2"]


def test_a_partial_throttled_fetch_counts_toward_the_breaker(tmp_path, adapter):
    from scraper.models import PartialFetch
    outcomes, calls = adapter
    for name in ("b0", "b1"):
        label = f"workday/{name}"
        outcomes[label] = lambda config, label=label: PartialFetch([job(label)], throttled=True)

    jobs, stats = run(SeenStore(str(tmp_path / "seen.json")))

    assert calls == ["workday/b0", "workday/b1"] and len(jobs) == 2
    assert stats["workday/b0"]["truncated"] and not stats["workday/b0"]["errors"]


def test_boards_that_left_the_config_are_forgotten(tmp_path, adapter):
    store = SeenStore(str(tmp_path / "seen.json"))
    store.polling["workday/gone"] = NOW.isoformat()
    run(store)
    assert "workday/gone" not in store.polling


def test_polling_times_survive_a_save(tmp_path, adapter):
    path = str(tmp_path / "seen.json")
    store = SeenStore(path)
    run(store)
    store.save()
    assert SeenStore(path).polling == store.polling != {}


def test_skipped_boards_keep_their_health_untouched(tmp_path, adapter):
    store = SeenStore(str(tmp_path / "seen.json"))
    store.health["workday/b6"] = {"consecutive_failures": 3, "zero_since": None,
                                  "warned_failures": False, "warned_zero": False}
    _, stats = run(store)
    assert stats["workday/b6"]["skipped"]

    health.record_run(store.health, stats)

    assert store.health["workday/b6"]["consecutive_failures"] == 3  # neither reset nor dropped
    assert "workday/b5" not in store.health  # and no zero-jobs streak started


def test_rate_limited_boards_warn_only_after_a_day_without_success():
    counters: dict = {}
    throttled = {"workday/td": {"fetched": 0, "errors": 1, "throttled": True,
                                "error": "HTTP 429 from td.wd3.myworkdayjobs.com"}}
    warnings = []
    for hour in range(24):  # far more than the 5 failures that warn for a real error
        warnings += health.record_run(counters, throttled, now=NOW + timedelta(hours=hour))
    assert warnings == []

    warnings = health.record_run(counters, throttled, now=NOW + timedelta(hours=24))
    assert len(warnings) == 1 and "rate-limited" in warnings[0]
    assert health.record_run(counters, throttled, now=NOW + timedelta(hours=25)) == []

    health.record_run(counters, {"workday/td": {"fetched": 5, "errors": 0}},
                      now=NOW + timedelta(hours=26))
    assert counters["workday/td"]["consecutive_failures"] == 0
    assert "failing_since" not in counters["workday/td"]
    assert "throttled" not in counters["workday/td"]


def test_one_success_restarts_the_rate_limit_clock():
    counters: dict = {}
    throttled = {"workday/td": {"fetched": 0, "errors": 1, "throttled": True}}
    health.record_run(counters, throttled, now=NOW)
    health.record_run(counters, {"workday/td": {"fetched": 5, "errors": 0}},
                      now=NOW + timedelta(hours=20))
    warnings = health.record_run(counters, throttled, now=NOW + timedelta(hours=30))
    assert warnings == []  # failing only since hour 30, not since hour 0
