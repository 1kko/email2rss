"""
Shared test fixtures.

Environment variables are set at module import time (before any project module
is imported) because `common.py`, `database.py`, and `feed_server.py` capture
config at import time via `load_dotenv()` and `os.getenv()`.
"""
# ruff: noqa: E402  — intentional: env vars must be set before project imports
from __future__ import annotations

import datetime
import email as email_mod
import os
import tempfile
import atexit
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Env setup — MUST run before importing anything from the project
# ---------------------------------------------------------------------------
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="email2rss_test_"))
(_TEST_DATA_DIR / "feed").mkdir(parents=True, exist_ok=True)
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

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

import database


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

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
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
    Helper: insert an email row.

    When `content` is omitted, the canonical multipart MIME sample is loaded
    and its Subject/Date/From headers are rewritten to match the caller's
    arguments. The rewritten From header uses the bare `sender` address
    (no display name) so tests can compute the GUID as
    `md5(subject + date_str + sender)` — matching what `database` and
    `feed_generator` compute from `msg["from"]`.
    """
    if content is None:
        msg = email_mod.message_from_bytes(_load_sample_eml())
        msg.replace_header("Subject", subject)
        msg.replace_header("Date", date_str)
        msg.replace_header("From", sender)
        content = msg.as_bytes()

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
