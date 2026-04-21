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
    _factory = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)

    monkeypatch.setattr(database, "engine", engine)

    # Set up FTS virtual table + delete trigger in the test engine
    with engine.connect() as conn:
        database._setup_fts(conn)
        conn.commit()

    session = _factory()

    # Expose a session factory that always returns *this* session so that
    # helpers (mark_read, mark_starred, …) operate on the same unit-of-work
    # as the test.  The factory is callable (matching production Session usage)
    # and the returned object supports the context-manager protocol used by
    # `with Session() as s:` without closing the underlying session.
    class _SameSessionFactory:
        """Always returns `session`; context-manager exit is a no-op."""

        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *args):
            # Do NOT close — the test fixture owns the session lifetime.
            return False

        # Delegate everything else to `session` so callers that do
        # `Session().query(...)` without a `with` block also work.
        def __getattr__(self, name):
            return getattr(session, name)

    monkeypatch.setattr(database, "Session", _SameSessionFactory())

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
    inline_images: dict[str, tuple[str, bytes]] | None = None,
):
    """
    Helper: insert an email row. When `content` is omitted, the canonical multipart
    MIME sample is loaded and headers rewritten. `inline_images` optionally builds
    a multipart/related wrapper with inline parts keyed by Content-ID.
    """
    if content is None:
        msg = email_mod.message_from_bytes(_load_sample_eml())
        msg.replace_header("Subject", subject)
        msg.replace_header("Date", date_str)
        msg.replace_header("From", sender)
        if inline_images:
            from email.mime.multipart import MIMEMultipart
            from email.mime.image import MIMEImage
            related = MIMEMultipart("related")
            for h in ("From", "To", "Subject", "Date", "MIME-Version"):
                if msg[h]:
                    related[h] = msg[h]
            # Preserve the original alternative body as the first related part
            alt_payload = msg.get_payload()
            if not isinstance(alt_payload, list):
                raise TypeError(
                    f"insert_email(inline_images=...) expected multipart base message; "
                    f"got {type(alt_payload).__name__}"
                )
            alternative = MIMEMultipart("alternative")
            for part in alt_payload:
                alternative.attach(part)
            related.attach(alternative)
            for cid, (ctype, raw_bytes) in inline_images.items():
                _maintype, _subtype = ctype.split("/", 1)
                img = MIMEImage(raw_bytes, _subtype=_subtype)
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline")
                related.attach(img)
            content = related.as_bytes()
        else:
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
