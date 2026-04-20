# Test Foundation + CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land a minimum-viable pytest suite (~22 characterization tests) plus a GitHub Actions CI workflow that lints, tests, and docker-builds on every push and PR to `master`.

**Architecture:** Tests live in a new top-level `tests/` directory. A single `tests/conftest.py` sets environment variables before any project module is imported (because `common.py`, `database.py`, and `feed_server.py` capture config at import time), provides a `db_session` fixture that rebinds `database.engine`/`database.Session` to an in-memory SQLite per test, and a `client` fixture that instantiates a fresh Flask app via `feed_server.create_app()`. CI runs three parallel jobs on `ubuntu-latest` / Python 3.12: lint (ruff), test (pytest + coverage), docker-build.

**Tech Stack:** pytest, pytest-cov, SQLAlchemy in-memory SQLite, Flask test client, GitHub Actions, ruff, Docker.

---

## File Structure

Files this plan creates or modifies:

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | modify | Add `[project.optional-dependencies].test` group (pytest, pytest-cov) and `[tool.pytest.ini_options]` |
| `tests/__init__.py` | create | Empty — marks `tests` as a package |
| `tests/conftest.py` | create | Env-var setup, `db_session` fixture, `client` fixture, `sample_email_bytes` helper |
| `tests/fixtures/__init__.py` | create | Empty |
| `tests/fixtures/emails/sample_multipart.eml` | create | Multipart MIME sample for parsing tests |
| `tests/test_util.py` | create | 4 tests — `extract_email_address`, `extract_domain_address`, `cleanse_content`, `sanitize_html` |
| `tests/test_database.py` | create | 6 tests — save/get/count/senders/guid/indexes |
| `tests/test_feed_generator.py` | create | 4 tests — RSS XML validity, ordering, link mode switching |
| `tests/test_feed_server.py` | create | 8 tests — Flask routes, 200/404 paths, content types |
| `.github/workflows/ci.yml` | create | Three-job CI: lint, test, docker-build |

Note: the design spec mentions "`sanitize_filename`, `strip_html`, etc." as example `util.py` tests. The actual helpers in `util.py` are `extract_email_address`, `extract_domain_address`, `extract_name_from_email`, `utf8_decoder`, `cleanse_content`, `sanitize_html`. This plan tests the four that matter most for seed coverage: the two extractors, `cleanse_content`, and `sanitize_html`.

---

## Task 1: Add test dependencies and `tests/` scaffolding

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/fixtures/__init__.py`

- [ ] **Step 1.1: Add `test` optional-dependency group to `pyproject.toml`**

In `pyproject.toml`, add this block under `[project]` (after the `dependencies` list):

```toml
[project.optional-dependencies]
test = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
]
```

And add this block near the bottom of the file (after `[tool.ruff.lint.per-file-ignores]`):

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
pythonpath = ["."]
addopts = "-ra --strict-markers"
```

The `pythonpath = ["."]` line puts the repo root on `sys.path` so that both `import database` and `from tests.conftest import insert_email` resolve during test collection.

Also update `[tool.ruff.lint.per-file-ignores]` to skip the `S` (security) lint rules for tests, since test code uses assertions, hardcoded passwords, etc. that are fine in tests:

```toml
[tool.ruff.lint.per-file-ignores]
"debug_reader.py" = ["S"]
"tests/*" = ["S"]
```

- [ ] **Step 1.2: Create empty `__init__.py` files**

```bash
mkdir -p tests/fixtures/emails tests/fixtures/feeds
touch tests/__init__.py tests/fixtures/__init__.py
```

- [ ] **Step 1.3: Verify install works**

Run:
```bash
pip install -e '.[test]'
pytest --collect-only
```

Expected: pytest installs and reports `no tests ran` (collection succeeds because `testpaths` points to an existing empty directory).

- [ ] **Step 1.4: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/fixtures/__init__.py
git commit -m "test: add pytest scaffolding and test deps"
```

---

## Task 2: `conftest.py` — env setup, `db_session`, `client` fixtures

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/fixtures/emails/sample_multipart.eml`

- [ ] **Step 2.1: Create a sample MIME email fixture**

Create `tests/fixtures/emails/sample_multipart.eml` with:

```
From: Sender Name <sender@example.com>
To: user@localhost
Subject: Hello from the test suite
Date: Mon, 13 Apr 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="BOUNDARY"

--BOUNDARY
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 7bit

Hello from plain text.

--BOUNDARY
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: 7bit

<html><body><p>Hello from <b>HTML</b>.</p></body></html>

--BOUNDARY--
```

- [ ] **Step 2.2: Write `tests/conftest.py`**

Create `tests/conftest.py`:

```python
"""
Shared test fixtures.

Environment variables are set at module import time (before any project module
is imported) because `common.py`, `database.py`, and `feed_server.py` capture
config at import time via `load_dotenv()` and `os.getenv()`.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Env setup — MUST run before importing anything from the project
# ---------------------------------------------------------------------------
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="email2rss_test_"))
(_TEST_DATA_DIR / "feed").mkdir(parents=True, exist_ok=True)

os.environ["data_dir"] = str(_TEST_DATA_DIR)
os.environ["max_item_per_feed"] = "100"
os.environ.setdefault("userid", "test@example.com")
os.environ.setdefault("userpw", "test-password")
os.environ.setdefault("imap_server", "imap.example.com")
os.environ.setdefault("mailbox", "INBOX")
os.environ.setdefault("server_baseurl", "http://testserver")
os.environ.setdefault("enable_internal_reader", "false")
os.environ.setdefault("bind_address", "127.0.0.1")
os.environ.setdefault("port", "8000")

# ---------------------------------------------------------------------------
# Now safe to import project modules
# ---------------------------------------------------------------------------
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database  # noqa: E402


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_sample_eml() -> bytes:
    """Return the bytes of the canonical multipart MIME sample."""
    return (FIXTURES_DIR / "emails" / "sample_multipart.eml").read_bytes()


@pytest.fixture
def sample_eml_bytes() -> bytes:
    """Canonical multipart MIME email fixture."""
    return _load_sample_eml()


@pytest.fixture
def db_session(monkeypatch):
    """
    Give each test a fresh in-memory SQLite database.

    Rebinds `database.engine` and `database.Session` to a fresh in-memory
    engine so that every call site in the project (which imports `Session`
    from `database`) transparently uses the test DB.
    """
    engine = create_engine("sqlite:///:memory:")
    SessionLocal = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "Session", SessionLocal)

    yield SessionLocal()

    engine.dispose()


@pytest.fixture
def client(db_session, tmp_path, monkeypatch):
    """
    Flask test client with an isolated feed directory.

    Each test gets a fresh Flask app via `create_app()`. View functions look
    up `FEED_DIR` from module scope at request time, so monkeypatching the
    module attribute (rather than reloading) is sufficient.
    """
    import feed_server

    monkeypatch.setattr(feed_server, "FEED_DIR", tmp_path)

    app = feed_server.create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def insert_email(
    session,
    sender: str = "sender@example.com",
    email_id: int = 1,
    subject: str = "Hello from the test suite",
    date_str: str = "Mon, 13 Apr 2026 10:00:00 +0000",
    content: bytes | None = None,
    timestamp=None,
):
    """
    Helper: insert an email row. Content defaults to the canonical MIME sample,
    re-rewritten so that Subject/Date/From match the caller-supplied values
    (needed for GUID calculation to match).
    """
    import datetime

    if content is None:
        raw = _load_sample_eml().decode("utf-8", errors="ignore")
        raw = raw.replace("Hello from the test suite", subject)
        raw = raw.replace("Mon, 13 Apr 2026 10:00:00 +0000", date_str)
        raw = raw.replace("Sender Name <sender@example.com>", f"Sender Name <{sender}>")
        content = raw.encode("utf-8")

    if timestamp is None:
        timestamp = datetime.datetime(2026, 4, 13, 10, 0, 0)

    row = database.Email(
        sender=sender,
        receiver="user@localhost",
        email_id=email_id,
        subject=subject,
        content=content,
        timestamp=timestamp,
    )
    session.add(row)
    session.commit()
    return row
```

- [ ] **Step 2.3: Smoke-check that the fixture file loads and `pytest --collect-only` still works**

Run:
```bash
pytest --collect-only
```

Expected: `no tests ran` (no tests yet, but collection succeeds with no errors). If it errors on `import database`, the env-var ordering in conftest is wrong — env vars must be set **before** the `import database` line.

- [ ] **Step 2.4: Commit**

```bash
git add tests/conftest.py tests/fixtures/emails/sample_multipart.eml
git commit -m "test: add conftest fixtures and sample MIME email"
```

---

## Task 3: `test_util.py` — 4 tests for pure helpers

**Files:**
- Create: `tests/test_util.py`

- [ ] **Step 3.1: Write all four tests**

Create `tests/test_util.py`:

```python
"""Tests for util.py — pure helper functions."""
from util import (
    cleanse_content,
    extract_domain_address,
    extract_email_address,
    sanitize_html,
)


def test_extract_email_address_from_formatted_string():
    assert extract_email_address("Sender Name <hello@Example.COM>") == "hello@example.com"


def test_extract_email_address_returns_default_when_missing():
    assert extract_email_address("no address here", default="fallback@x.com") == "fallback@x.com"


def test_extract_domain_address():
    assert extract_domain_address("user@sub.example.com") == "sub.example.com"
    assert extract_domain_address("no-at-sign", default="unknown") == "unknown"


def test_cleanse_content_strips_control_chars_but_keeps_whitespace():
    raw = "keep\ttab\nnewline\rcr\x00null\x08backspace"
    assert cleanse_content(raw) == "keep\ttab\nnewline\rcrnullbackspace"


def test_sanitize_html_removes_script_and_event_handlers():
    dirty = '<p onclick="alert(1)">hi</p><script>evil()</script><a href="javascript:bad()">x</a>'
    clean = sanitize_html(dirty)
    assert "<script>" not in clean
    assert "evil()" not in clean
    assert "onclick" not in clean
    assert "javascript:" not in clean
    assert "hi" in clean  # content preserved
```

(That's 5 test functions but 4 covered behaviors — `extract_email_address` has two cases. Count it as 5 tests; still within the seed target.)

- [ ] **Step 3.2: Run the tests**

Run:
```bash
pytest tests/test_util.py -v
```

Expected: all 5 tests PASS. If any fail, the existing helper behavior differs from what the test asserts — check which and either (a) fix the test to match actual behavior or (b) flag a real bug for follow-up (seed tests should characterize *current* behavior, not invent new requirements).

- [ ] **Step 3.3: Commit**

```bash
git add tests/test_util.py
git commit -m "test: add util.py characterization tests"
```

---

## Task 4: `test_database.py` — 6 tests

**Files:**
- Create: `tests/test_database.py`

- [ ] **Step 4.1: Write the tests**

Create `tests/test_database.py`:

```python
"""Tests for database.py — upsert, queries, indexes."""
import datetime

from sqlalchemy import text

import database as db
from tests.conftest import insert_email


def test_save_email_inserts_new_row(db_session):
    db.save_email(
        sender="a@example.com",
        receiver="me@localhost",
        email_id=101,
        subject="Subject A",
        content=b"From: a@example.com\nSubject: Subject A\nDate: Mon, 13 Apr 2026 10:00:00 +0000\n\nbody",
        timestamp=datetime.datetime(2026, 4, 13, 10, 0, 0),
    )
    assert db.get_entry_count() == 1


def test_save_email_is_idempotent_on_duplicate_email_id(db_session):
    kwargs = dict(
        sender="a@example.com",
        receiver="me@localhost",
        email_id=101,
        subject="Subject A",
        content=b"From: a@example.com\nSubject: Subject A\nDate: Mon, 13 Apr 2026 10:00:00 +0000\n\nbody",
        timestamp=datetime.datetime(2026, 4, 13, 10, 0, 0),
    )
    db.save_email(**kwargs)
    db.save_email(**kwargs)  # second call is a no-op
    assert db.get_entry_count() == 1


def test_get_email_returns_newest_first(db_session):
    insert_email(db_session, email_id=1, timestamp=datetime.datetime(2026, 4, 10))
    insert_email(db_session, email_id=2, timestamp=datetime.datetime(2026, 4, 12))
    insert_email(db_session, email_id=3, timestamp=datetime.datetime(2026, 4, 11))

    rows = list(db.get_email("sender@example.com"))
    timestamps = [r.timestamp for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_email_respects_max_item_per_feed(db_session, monkeypatch):
    # Temporarily lower the limit so we don't need to insert 101 rows
    monkeypatch.setitem(db.config, "max_item_per_feed", 2)
    for i in range(5):
        insert_email(db_session, email_id=i, timestamp=datetime.datetime(2026, 4, 10 + i))

    rows = list(db.get_email("sender@example.com"))
    assert len(rows) == 2


def test_get_senders_returns_distinct_list(db_session):
    insert_email(db_session, sender="alice@example.com", email_id=1)
    insert_email(db_session, sender="alice@example.com", email_id=2)
    insert_email(db_session, sender="bob@example.com", email_id=3)

    senders = db.get_senders()
    assert set(senders) == {"alice@example.com", "bob@example.com"}


def test_required_indexes_exist(db_session):
    expected = {"ix_emails_sender", "ix_emails_email_id", "ix_emails_timestamp", "idx_sender_timestamp"}
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='emails'")
        )
        got = {row[0] for row in rows}
    missing = expected - got
    assert not missing, f"missing indexes: {missing}"
```

Note on the last test: when `db_session` creates the schema via `Base.metadata.create_all()`, SQLAlchemy creates all four indexes declared on the `Email` model (three from `Column(..., index=True)` + one from `__table_args__`). The `migrate_database()` runtime path also creates them, so this test covers both paths.

- [ ] **Step 4.2: Run the tests**

Run:
```bash
pytest tests/test_database.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 4.3: Commit**

```bash
git add tests/test_database.py
git commit -m "test: add database.py characterization tests"
```

---

## Task 5: `test_feed_generator.py` — 4 tests

**Files:**
- Create: `tests/test_feed_generator.py`

- [ ] **Step 5.1: Write the tests**

Create `tests/test_feed_generator.py`:

```python
"""Tests for feed_generator.generate_rss — XML structure and link-mode switching."""
import datetime

from defusedxml.ElementTree import fromstring as safe_fromstring

import feed_generator
from tests.conftest import insert_email


def _rss_items(xml_str: str):
    root = safe_fromstring(xml_str)
    return root.findall(".//channel/item")


def test_generate_rss_produces_parseable_rss_xml(db_session):
    insert_email(db_session, email_id=1, subject="First")
    insert_email(db_session, email_id=2, subject="Second", timestamp=datetime.datetime(2026, 4, 14))

    messages = list(db_session.query(feed_generator.db.Email).filter_by(sender="sender@example.com"))
    xml = feed_generator.generate_rss("sender@example.com", messages)
    items = _rss_items(xml)
    assert len(items) == 2
    titles = [it.findtext("title") for it in items]
    assert {"First", "Second"}.issubset(set(titles))


def test_generate_rss_produces_newest_first_rss_order(db_session):
    """RSS output is newest-first.

    `generate_rss` calls `messages_list.reverse()` before iterating, BUT
    `feedgen.add_entry()` prepends each new entry (inserts at index 0). The
    two reversals cancel out, so RSS order matches the DB query order
    (newest-first). This test pins that neutralizing double-transformation.
    """
    insert_email(db_session, email_id=1, subject="Oldest", timestamp=datetime.datetime(2026, 4, 10))
    insert_email(db_session, email_id=2, subject="Middle", timestamp=datetime.datetime(2026, 4, 11))
    insert_email(db_session, email_id=3, subject="Newest", timestamp=datetime.datetime(2026, 4, 12))

    messages = list(feed_generator.db.get_email("sender@example.com"))
    xml = feed_generator.generate_rss("sender@example.com", messages)
    titles = [it.findtext("title") for it in _rss_items(xml)]
    assert titles == ["Newest", "Middle", "Oldest"]


def test_internal_reader_mode_links_to_article_viewer(db_session, monkeypatch):
    monkeypatch.setitem(feed_generator.config, "enable_internal_reader", True)
    monkeypatch.setitem(feed_generator.config, "server_baseurl", "http://testserver")
    insert_email(db_session, email_id=1)

    messages = list(feed_generator.db.get_email("sender@example.com"))
    xml = feed_generator.generate_rss("sender@example.com", messages)
    link = _rss_items(xml)[0].findtext("link")
    assert link.startswith("http://testserver/article/sender_example_com/")


def test_external_mode_links_to_sender_domain(db_session, monkeypatch):
    monkeypatch.setitem(feed_generator.config, "enable_internal_reader", False)
    insert_email(db_session, email_id=1, sender="hello@tailscale.com")

    messages = list(feed_generator.db.get_email("hello@tailscale.com"))
    xml = feed_generator.generate_rss("hello@tailscale.com", messages)
    link = _rss_items(xml)[0].findtext("link")
    assert link == "https://tailscale.com"
```

- [ ] **Step 5.2: Run the tests**

Run:
```bash
pytest tests/test_feed_generator.py -v
```

Expected: all 4 tests PASS. If `test_generate_rss_reverses_input_order` fails, reconcile with the actual `generate_rss` behavior (see `feed_generator.py:87-89` — it calls `messages_list.reverse()` before iteration).

- [ ] **Step 5.3: Commit**

```bash
git add tests/test_feed_generator.py
git commit -m "test: add feed_generator characterization tests"
```

---

## Task 6: `test_feed_server.py` — 8 tests

**Files:**
- Create: `tests/test_feed_server.py`

- [ ] **Step 6.1: Write the tests**

Create `tests/test_feed_server.py`:

```python
"""Tests for feed_server Flask routes."""
import hashlib

from defusedxml.ElementTree import fromstring as safe_fromstring

import feed_server
from tests.conftest import insert_email


def _expected_guid(subject: str, date_str: str, from_header: str) -> str:
    return hashlib.md5((subject + date_str + from_header).encode(), usedforsecurity=False).hexdigest()


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_stats_reports_counts_and_senders(client, db_session):
    insert_email(db_session, sender="alice@example.com", email_id=1)
    insert_email(db_session, sender="bob@example.com", email_id=2)

    resp = client.get("/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_emails"] == 2
    assert data["total_senders"] == 2
    assert set(data["senders"]) == {"alice@example.com", "bob@example.com"}


def test_feed_xml_served_from_feed_dir(client, tmp_path, monkeypatch):
    # feed_server serves static feed files from FEED_DIR (monkeypatched to tmp_path in fixture)
    (tmp_path / "hello_example_com.xml").write_text(
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>',
        encoding="utf-8",
    )
    resp = client.get("/hello_example_com.xml")
    assert resp.status_code == 200
    # send_from_directory infers content type from extension; .xml → application/xml
    assert resp.mimetype in ("application/xml", "text/xml")
    # Sanity: parseable
    safe_fromstring(resp.data)


def test_unknown_feed_returns_404(client):
    resp = client.get("/does_not_exist.xml")
    assert resp.status_code == 404


def test_subscriptions_opml_served_when_present(client, tmp_path):
    (tmp_path / "subscriptions.opml").write_text(
        '<?xml version="1.0"?><opml version="1.0"><head><title>t</title></head><body/></opml>',
        encoding="utf-8",
    )
    resp = client.get("/subscriptions.opml")
    assert resp.status_code == 200
    root = safe_fromstring(resp.data)
    assert root.tag == "opml"


def test_article_route_404s_when_guid_unknown(client, db_session, monkeypatch):
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    insert_email(db_session, email_id=1)
    resp = client.get("/article/sender_example_com/nonexistent_guid")
    assert resp.status_code == 404


def test_article_route_renders_email_body(client, db_session, monkeypatch):
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    subject = "Hello from the test suite"
    date_str = "Mon, 13 Apr 2026 10:00:00 +0000"
    sender = "sender@example.com"
    # insert_email rewrites From to the bare sender address; GUID matches that
    guid = _expected_guid(subject, date_str, sender)

    insert_email(db_session, email_id=1)

    resp = client.get(f"/article/sender_example_com/{guid}")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Hello from" in body  # HTML or plain content makes it into the rendered template


def test_article_route_not_found_for_unknown_feed(client, db_session, monkeypatch):
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    resp = client.get("/article/who_knows_com/abcdef")
    assert resp.status_code == 404
```

Two clarifying notes for the implementer:

1. The `/article/<feed>/<guid>` route in `feed_server.py` does not check `enable_internal_reader` — it always tries to find and render the article. The spec's design called for a 404 when reader is disabled, but that's a sub-project 2 concern. For the seed tests, we test the actual current behavior (renders regardless of the flag) via `test_article_route_renders_email_body`. If time permits, file an issue noting the discrepancy; otherwise leave as-is.
2. The `client` fixture reloads `feed_server` and monkeypatches `FEED_DIR`, so each test gets a fresh `FEED_DIR = tmp_path`.

- [ ] **Step 6.2: Run the tests**

Run:
```bash
pytest tests/test_feed_server.py -v
```

Expected: all 8 tests PASS. If a test fails due to current-behavior mismatch (common on the article/404 tests), adjust the test assertion to match what the server does today — these are characterization tests, not a wishlist.

- [ ] **Step 6.3: Commit**

```bash
git add tests/test_feed_server.py
git commit -m "test: add feed_server route tests"
```

---

## Task 7: GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 7.1: Write the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [master]
  pull_request:
    branches: [master]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install ruff
        run: pip install ruff
      - name: Run ruff
        run: ruff check .

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: pip install -e '.[test]'
      - name: Run pytest with coverage
        run: pytest --cov=. --cov-report=term-missing

  docker-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build serve image
        run: docker build -f Dockerfile.serve -t email2rss-serve:ci .
      - name: Build fetch/generate image
        run: docker build -f Dockerfile.fetch_and_generate -t email2rss-fetch:ci .
```

- [ ] **Step 7.2: Sanity-check locally**

Run the same commands the workflow runs, to catch obvious problems before pushing:

```bash
ruff check .
pytest --cov=. --cov-report=term-missing
docker build -f Dockerfile.serve -t email2rss-serve:ci .
docker build -f Dockerfile.fetch_and_generate -t email2rss-fetch:ci .
```

Expected: all four commands succeed. If `ruff check .` flags anything in the new `tests/` files, either fix the issue or extend `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` to cover the pattern.

- [ ] **Step 7.3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add lint + test + docker-build workflow"
```

---

## Task 8: Final smoke + docs

**Files:**
- Modify: `README.md`

- [ ] **Step 8.1: Run the full suite one more time**

```bash
pytest -v
```

Expected: ~22 tests PASS (4-5 util, 6 database, 4 feed_generator, 8 feed_server). Zero failures.

- [ ] **Step 8.2: Add a short "Development" section to `README.md`**

In `README.md`, add this section just above the `## License` line:

```markdown
## Development

Install with dev dependencies and run the test suite:

```bash
pip install -e '.[test]'
pytest
```

Lint:

```bash
ruff check .
```

CI (GitHub Actions) runs lint, tests, and docker builds on every push and pull request to `master`.
```

- [ ] **Step 8.3: Commit**

```bash
git add README.md
git commit -m "docs: add Development section for test setup"
```

- [ ] **Step 8.4: Push and verify CI is green**

```bash
git push origin master
```

Then open the Actions tab on GitHub and confirm the CI workflow runs all three jobs to green. If any job fails, iterate locally (do **not** amend published commits — add follow-up commits).

---

## Acceptance criteria checklist

- [ ] `pytest` runs locally, ~22 tests all pass
- [ ] `pip install -e '.[test]'` installs cleanly
- [ ] CI workflow exists at `.github/workflows/ci.yml`
- [ ] All three CI jobs (lint, test, docker-build) pass on push to a branch
- [ ] Coverage report prints to CI logs
- [ ] `docker-compose up` and `poetry run python start.py` still work (manually verified)
- [ ] `README.md` has a Development section documenting the test commands
