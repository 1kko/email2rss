"""
Shared test fixtures.

Environment variables are set at module import time (before any project module
is imported) because `common.py`, `database.py`, and `feed_server.py` capture
config at import time via `load_dotenv()` and `os.getenv()`.
"""
# ruff: noqa: E402  — intentional: env vars must be set before project imports
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
