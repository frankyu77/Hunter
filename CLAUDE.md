# CLAUDE.md

Hunter polls job sources on a cron, diffs against state, and pushes never-seen
postings to Telegram. See `README.md` for setup (Telegram bot, secrets, adding
sources). This file covers the conventions and invariants that are easy to
break without noticing.

## Commands

```bash
pip install -r requirements.txt
python -m scraper.main --dry-run   # full pipeline, prints instead of sending
ruff check .                       # lint (line-length 100, target py312)
pytest                             # fully offline; HTTP is mocked
```

## Architecture

```
sources.yaml -> FETCH -> NORMALIZE -> DEDUP -> FILTER -> NOTIFY (Telegram)
                (adapters)  (Job)  (seen_jobs.json) (predicates)
```

`scraper/main.py` orchestrates; each stage is its own function so tests can
exercise the seams. `scraper/models.py` defines `Job`, the single shape the
whole pipeline speaks — adapters normalize into it, nothing downstream knows
which source a job came from.

`scraper/adapters/__init__.py` holds `REGISTRY`, mapping a `sources.yaml`
`type:` string to a `fetch(config: dict) -> list[Job]` function. Dispatch is
purely by type string; the orchestrator never special-cases a source.

## Adding a source type

1. New module in `scraper/adapters/` exposing `fetch(config) -> list[Job]`.
2. Register it in `REGISTRY`.
3. Update `test_registry_has_no_stale_entries` in `tests/test_adapters.py`
   (it asserts the exact key set, so it fails until you do).
4. Record a real response into `tests/fixtures/` and add a mapping test using
   `responses`. Tests never touch the network.
5. Module docstring should name the exact endpoint used and, if there is a
   tempting wrong door (a JS-rendered page, an authenticated API), say so —
   see `adapters/ashby.py`.

## Invariants

These are deliberate, and a change that violates one is a bug even if the
tests still pass:

- **Never-miss beats never-duplicate.** `store.add(job)` happens only *after*
  a send succeeds. A crash between the two re-sends a harmless duplicate; the
  reverse order silently loses a job forever.
- **Bulkhead per source.** One source raising must never sink the run —
  `fetch_all` wraps each source in its own try/except.
- **Retry 5xx and timeouts, fail fast on 4xx.** A 4xx means the source config
  is wrong and retrying can't fix it.
- **`Job.id` must be stable across runs** — `"{type}:{company}:{ats_job_id}"`,
  falling back to a URL hash. Changing an id format re-notifies every existing
  posting from that source.
- **Silent seeding.** An empty store (first run ever) and a source with no
  previously-seen jobs (just added) both get recorded without notifying,
  otherwise the entire backlog floods the chat.
- **Filtered-out jobs are still recorded as seen**, after notify — otherwise
  they re-enter the diff as new on every run forever.
- **Prune runs last**, after notifications and state writes, so it can't race
  the dedup.

## State file

`seen_jobs.json` is committed state, written and pushed by the
`Poll job sources` workflow on `main`. Don't hand-edit or reformat it, and
expect it to conflict on long-lived branches — take `main`'s version.

## Style

Standard library plus `requests` and `pyyaml`; no framework. `Job` is a frozen
dataclass. Module docstrings explain *why* the module is shaped the way it is,
not what the functions do — match that when adding files.
