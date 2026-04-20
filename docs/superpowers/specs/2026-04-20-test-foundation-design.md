# Test Foundation + CI

**Status:** Approved design, ready for implementation plan
**Date:** 2026-04-20
**Scope:** Sub-project 1 of 4 in the broader reliability/feature improvement initiative

## Purpose

Establish a regression safety net before the three invasive sub-projects that follow (reader security hardening, fetcher reliability, reader features). Today the repo has zero automated tests and no CI; any refactor has to be verified by hand. This sub-project delivers the minimum viable test and CI infrastructure so that future changes can be landed with confidence.

## Non-goals

- High coverage percentage. This is a seed, not a comprehensive suite.
- Testing the IMAP fetcher. Deferred to sub-project 3, which restructures the fetcher.
- Pre-commit hooks, Codecov uploads, or test caching. All YAGNI for a personal-scale project.
- Python version matrix. Production runs on Python 3.12 (per `Dockerfile.*`); CI locks to 3.12 only.

## Architecture

### Directory layout

```
email2rss/
├── tests/
│   ├── conftest.py          # shared fixtures
│   ├── fixtures/
│   │   ├── emails/          # sample .eml files for parsing tests
│   │   └── feeds/           # golden RSS XML for feed_generator assertions
│   ├── test_util.py
│   ├── test_database.py
│   ├── test_feed_generator.py
│   └── test_feed_server.py
├── .github/
│   └── workflows/
│       └── ci.yml
└── pyproject.toml           # adds [project.optional-dependencies].test
```

### Dependencies

Added under a new `[project.optional-dependencies].test` group in `pyproject.toml`:

- `pytest` — test runner
- `pytest-cov` — coverage reporting (terminal output only, not gated)

No other new runtime or dev dependencies.

### Fixtures (in `tests/conftest.py`)

- `db_session` — in-memory SQLite (`sqlite:///:memory:`), applies the project schema, tears down after each test.
- `client` — Flask test client, wired to `db_session` and a `tmp_path` `data_dir` so feed XML writes land in an isolated directory per test.
- `sample_email(sender, subject, body, date)` — factory that inserts a row into `db_session` and returns the model instance for assertions.

Fixtures are function-scoped by default. Tests that need isolated DB state get it automatically.

**Known constraint:** `database.py` creates its SQLAlchemy `engine` at module import time, reading `data_dir` from `common.config`. The `db_session` fixture handles this by setting `data_dir` to a `tmp_path` *before* importing `database` (via environment variable or a pre-import config override), then rebinding the engine to `sqlite:///:memory:` for speed. No refactor of `database.py` is required for seed tests; the plan will pin down the exact mechanism.

## CI workflow

`.github/workflows/ci.yml` — three jobs running in parallel on push and pull_request to `master`:

| Job            | Steps                                                     |
|----------------|-----------------------------------------------------------|
| `lint`         | checkout → setup-python 3.12 → `pip install ruff` → `ruff check .` |
| `test`         | checkout → setup-python 3.12 → `pip install -e '.[test]'` → `pytest --cov=. --cov-report=term-missing` |
| `docker-build` | checkout → `docker build -f Dockerfile.serve .` → `docker build -f Dockerfile.fetch_and_generate .` |

**Design choices:**

- Parallel jobs, not a matrix. Lint, test, and docker build are independent concerns; parallel execution is faster and failure messages are clearer than a fanned-out matrix.
- Docker builds verify only — no registry push, no credentials required in CI.
- Coverage is printed, not gated. Threshold enforcement is a future concern once the suite matures.
- No pip or Docker layer caching. Full CI runs should complete in under two minutes; caching complexity is not yet earned.

## Seed test inventory

Target: ~22 tests producing first green CI.

### `test_util.py` (~4 tests)

- `sanitize_filename` strips unsafe characters, handles unicode, preserves extension
- `strip_html` removes tags, preserves text, handles malformed HTML
- GUID generation is deterministic for identical input and differs for different input

### `test_database.py` (~6 tests)

- Upsert a new email → row inserted
- Upsert duplicate (same `email_id`) → no-op, count unchanged
- Query emails by sender returns newest-first
- Query respects `max_item_per_feed` limit
- Distinct senders list matches inserted set
- Schema creates required indexes (spot-check via `PRAGMA index_list`)

### `test_feed_generator.py` (~4 tests)

- Given three fixture rows, generates valid RSS 2.0 XML (parse roundtrip via `defusedxml`)
- Items are ordered newest-first
- Item links point to `/article/<feed>/<guid>` when `enable_internal_reader=true`
- Item links point to the sender domain when `enable_internal_reader=false`

### `test_feed_server.py` (~8 tests)

- `GET /health` → 200, `{"status": "ok"}`
- `GET /stats` → 200, contains total and senders list
- `GET /<feed>.xml` → 200, `application/rss+xml`, valid XML
- `GET /<feed>.xml` for unknown feed → 404
- `GET /subscriptions.opml` → 200, valid OPML
- `GET /article/<feed>/<guid>` with reader disabled → 404
- `GET /article/<feed>/<guid>` with reader enabled, valid row → 200, body contains email HTML
- `GET /article/<feed>/<guid>` with reader enabled, unknown guid → 404

## IMAP testing approach

Not in scope for this sub-project. When sub-project 3 restructures the fetcher, IMAP will be mocked via `unittest.mock` patching of `imaplib.IMAP4_SSL`. No architectural changes are needed now.

## Acceptance criteria

1. `pytest` runs locally and all ~22 tests pass.
2. Pushing to a branch triggers CI; all three jobs (lint, test, docker-build) complete green.
3. Coverage report prints to CI logs.
4. `pyproject.toml` installs `[project.optional-dependencies].test` cleanly with `pip install -e '.[test]'`.
5. No regressions: existing `docker-compose up` and `poetry run python start.py` continue to work unchanged.

## Out of scope (explicitly deferred)

- Integration tests hitting a real IMAP server
- Fetcher unit tests (`email_fetcher.py`) — covered in sub-project 3
- Tests for security headers / CSP — covered in sub-project 2
- Tests for read/unread state and FTS search — covered in sub-project 4
- Python version matrix beyond 3.12
- Codecov / coverage gate
- Pre-commit hooks
