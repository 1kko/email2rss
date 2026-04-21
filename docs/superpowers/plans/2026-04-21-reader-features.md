# Reader Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read/unread + starred state and FTS5 full-text search to the internal reader. Dwell-time auto-read (client-side, default 5 s), manual "mark unread" button, star toggle, filter chips on article lists, dedicated `/search` page with snippet highlighting.

**Architecture:** Two new indexed columns (`is_read`, `is_starred`) on the existing `emails` table. Standalone FTS5 virtual table (`emails_fts`) keeping its own copy of `subject` + HTML-stripped `body_text`, rowid-aligned to `Email.id`. An `AFTER DELETE` trigger keeps FTS in sync on deletes; inserts go through an extended `save_email()` that strips HTML via existing `bleach` and writes to FTS explicitly. New REST-ish routes for read/star state (POST to set, DELETE to clear) guarded by an Origin-header same-origin check. Dwell-timer lives in `static/reader.js` (fired from the outer page, not the sandboxed iframe).

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Flask, Jinja2, SQLite FTS5 (built into SQLite), `bleach` (already a dep from sub-project 2), pytest with Flask test client.

---

## Scope Check

The spec covers three features (read/unread, starred, FTS search) bundled because they share DB migration territory. Each is modest on its own; together they fit one sub-project. Not decomposing further.

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `common.py` | modify | Add `read_after_seconds` config entry (default 5) |
| `database.py` | modify | New columns on Email; extend `migrate_database()` to add columns + FTS table + delete trigger + one-time backfill; extend `save_email()` to write FTS row; add `mark_read`, `mark_starred`, `get_emails_filtered`, `search_emails`, `get_email_by_guid_with_state`, `SearchSyntaxError` |
| `reader.py` | modify | Add `extract_plain_text(msg)` that returns subject-stripped plain text for FTS indexing |
| `feed_server.py` | modify | Add routes: `POST/DELETE /article/<feed>/<guid>/read`, `POST/DELETE /article/<feed>/<guid>/star`, `GET /search`; extend `/article` and `/article/<feed>` with `?filter=` param; Origin-header CSRF check helper; extend `view_article` to pass read/starred/read_after_seconds to template |
| `templates/base.html` | modify | Add `<nav class="site-nav">` with "All Articles" link + search form |
| `templates/article_list.html` | modify | Add filter chips + unread/starred styling markers on list items |
| `templates/article.html` | modify | Add data attributes for JS + article-actions toolbar (star button, unread button) |
| `templates/search_results.html` | **create** | Search results page |
| `static/reader.js` | modify | Replace the single-comment file with dwell timer + star button + unread button handlers |
| `static/reader.css` | modify | Add ~30 lines for site-nav, filter-chips, unread bold, star color, article-actions, search snippet |
| `tests/test_database.py` | modify | Add ~14 tests for new DB helpers, FTS behavior, migration |
| `tests/test_feed_server.py` | modify | Add ~10 tests for new routes, filter param, Origin check, search page |

No new dependencies.

---

## Task 1: DB schema — columns, FTS table, migration, save_email extension

**Files:**
- Modify: `database.py`
- Modify: `reader.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1.1: Add `is_read` and `is_starred` columns to the `Email` model**

In `database.py`, in the `Email` class definition (around line 19), add two columns alongside the existing ones:

```python
from sqlalchemy import Boolean

class Email(Base):
    __tablename__ = "emails"

    id = Column(Integer, primary_key=True)
    receiver = Column(String)
    sender = Column(String, index=True)
    email_id = Column(Integer, unique=True, index=True)
    subject = Column(Text)
    content = Column(BLOB)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    is_read = Column(Boolean, default=False, nullable=False, server_default="0", index=True)
    is_starred = Column(Boolean, default=False, nullable=False, server_default="0", index=True)

    __table_args__ = (
        Index('idx_sender_timestamp', 'sender', 'timestamp'),
    )
```

`Boolean` must be imported from SQLAlchemy. Add to the existing `from sqlalchemy import ...` line.

- [ ] **Step 1.2: Add `extract_plain_text` helper to `reader.py`**

Append to `reader.py`:

```python
import bleach


def extract_plain_text(msg) -> str:
    """
    Return HTML-stripped plain text for FTS indexing.

    Prefers text/plain parts; falls back to HTML→plain via bleach.
    Returns empty string if neither part is present.
    """
    body_html, _cid_map = extract_body_and_cid_map(msg)
    if not body_html:
        return ""
    # If body_html is actually plain text wrapped in <pre>, strip the <pre> wrapper
    # by running bleach with no allowed tags — bleach strips all markup.
    return bleach.clean(body_html, tags=[], strip=True)
```

- [ ] **Step 1.3: Extend `migrate_database()` to add columns + FTS table + trigger**

In `database.py`, replace the body of `migrate_database()`:

```python
def migrate_database():
    """
    Migrate existing database to add columns, indexes, and FTS5 table if missing.
    Safe to run on both new and existing databases.
    """
    logging.info("Checking database schema and indexes...")

    # Create tables if they don't exist (picks up new is_read/is_starred columns on fresh DBs)
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        # Check existing columns — ALTER TABLE ADD COLUMN on existing databases
        existing_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(emails)"))
        }
        if "is_read" not in existing_cols:
            logging.info("Adding column: is_read")
            conn.execute(text(
                "ALTER TABLE emails ADD COLUMN is_read BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "is_starred" not in existing_cols:
            logging.info("Adding column: is_starred")
            conn.execute(text(
                "ALTER TABLE emails ADD COLUMN is_starred BOOLEAN NOT NULL DEFAULT 0"
            ))

        # Existing index check (preserved from the pre-sub-project-4 migration)
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='emails'")
        )
        existing_indexes = {row[0] for row in result}
        required_indexes = {
            'ix_emails_sender': 'CREATE INDEX IF NOT EXISTS ix_emails_sender ON emails (sender)',
            'ix_emails_email_id': 'CREATE UNIQUE INDEX IF NOT EXISTS ix_emails_email_id ON emails (email_id)',
            'ix_emails_timestamp': 'CREATE INDEX IF NOT EXISTS ix_emails_timestamp ON emails (timestamp)',
            'ix_emails_is_read': 'CREATE INDEX IF NOT EXISTS ix_emails_is_read ON emails (is_read)',
            'ix_emails_is_starred': 'CREATE INDEX IF NOT EXISTS ix_emails_is_starred ON emails (is_starred)',
            'idx_sender_timestamp': 'CREATE INDEX IF NOT EXISTS idx_sender_timestamp ON emails (sender, timestamp)',
        }
        for index_name, create_sql in required_indexes.items():
            if index_name not in existing_indexes:
                logging.info(f"Creating index: {index_name}")
                conn.execute(text(create_sql))

        # FTS5 virtual table (standalone — keeps its own copy of subject+body_text)
        fts_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='emails_fts'"
        )).fetchone() is not None
        if not fts_exists:
            logging.info("Creating FTS5 virtual table: emails_fts")
            conn.execute(text(
                "CREATE VIRTUAL TABLE emails_fts USING fts5("
                "subject, body_text, "
                "tokenize='unicode61 remove_diacritics 2')"
            ))

        # Delete trigger: when an email row is deleted, delete the matching FTS row.
        # (Insert-side sync is handled in save_email, not a trigger, because
        # body_text requires Python HTML stripping.)
        trigger_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='emails_after_delete'"
        )).fetchone() is not None
        if not trigger_exists:
            logging.info("Creating trigger: emails_after_delete")
            conn.execute(text(
                "CREATE TRIGGER emails_after_delete AFTER DELETE ON emails "
                "BEGIN DELETE FROM emails_fts WHERE rowid = old.id; END"
            ))

        conn.commit()

        # Backfill FTS if table is empty but main table has rows (one-time on upgrade)
        fts_count = conn.execute(text("SELECT COUNT(*) FROM emails_fts")).scalar()
        main_count = conn.execute(text("SELECT COUNT(*) FROM emails")).scalar()
        if fts_count == 0 and main_count > 0:
            logging.info(f"Backfilling FTS index for {main_count} existing emails...")
            _backfill_fts_index(conn)

        logging.info("Database migration completed successfully")


def _backfill_fts_index(conn):
    """Populate emails_fts from existing emails. Called once on upgrade."""
    import reader  # local import to avoid circular dependency

    rows = conn.execute(text("SELECT id, subject, content FROM emails")).fetchall()
    for row_id, subject, content in rows:
        try:
            msg = email.message_from_bytes(content)
            body_text = reader.extract_plain_text(msg)
        except Exception:
            body_text = ""
            logging.warning(f"FTS backfill: failed to extract body_text for id={row_id}")
        conn.execute(
            text("INSERT INTO emails_fts(rowid, subject, body_text) VALUES (:id, :s, :b)"),
            {"id": row_id, "s": subject or "", "b": body_text},
        )
    conn.commit()
    logging.info(f"FTS backfill complete: {len(rows)} rows indexed")
```

- [ ] **Step 1.4: Extend `save_email` to write an FTS row**

In `database.py`, modify the `save_email` function. Replace the current function body (around line 101):

```python
def save_email(
    sender: str,
    receiver: str,
    email_id: int,
    subject: str,
    content: bytes,
    timestamp: datetime,
):
    """Save an email to the database and its FTS index row."""
    import reader  # local import to avoid circular at module load

    with Session() as session:
        existing_email = session.query(Email).filter_by(email_id=email_id).first()
        if existing_email is None:
            new_email = Email(
                sender=sender,
                receiver=receiver,
                email_id=email_id,
                subject=subject,
                content=content,
                timestamp=timestamp,
            )
            session.add(new_email)
            session.commit()
            # After commit we know new_email.id — write matching FTS row
            try:
                msg = email.message_from_bytes(content)
                body_text = reader.extract_plain_text(msg)
            except Exception:
                body_text = ""
                logging.warning(f"save_email: failed to extract body_text for email_id={email_id}")
            session.execute(
                text("INSERT INTO emails_fts(rowid, subject, body_text) VALUES (:id, :s, :b)"),
                {"id": new_email.id, "s": subject or "", "b": body_text},
            )
            session.commit()
        else:
            print(f"Email with id {email_id} already exists. Discarding.")
```

The rest of `database.py` (get_email, get_senders, etc.) is unchanged.

- [ ] **Step 1.5: Write failing tests for schema + FTS setup**

Append to `tests/test_database.py`:

```python
def test_email_model_has_is_read_and_is_starred_columns(db_session):
    """Fresh in-memory DB should have the new columns with False defaults."""
    insert_email(db_session, email_id=1)
    row = db_session.query(db.Email).first()
    assert row.is_read is False
    assert row.is_starred is False


def test_fts_table_exists_after_migration(db_session):
    """emails_fts virtual table should exist after migrate_database."""
    with db.engine.connect() as conn:
        row = conn.execute(
            db_text("SELECT name FROM sqlite_master WHERE type='table' AND name='emails_fts'")
        ).fetchone()
    assert row is not None


def test_fts_index_populated_by_save_email(db_session):
    """save_email should also insert into emails_fts so search finds the new row."""
    db.save_email(
        sender="s@example.com",
        receiver="me@localhost",
        email_id=200,
        subject="Indexable subject",
        content=b"From: s@example.com\nSubject: Indexable subject\nDate: Mon, 13 Apr 2026 10:00:00 +0000\n\nlookup me please",
        timestamp=datetime.datetime(2026, 4, 13),
    )
    with db.engine.connect() as conn:
        row = conn.execute(
            db_text("SELECT subject, body_text FROM emails_fts WHERE rowid = (SELECT id FROM emails WHERE email_id = 200)")
        ).fetchone()
    assert row is not None
    assert "Indexable subject" in row[0]
    assert "lookup me please" in row[1]


def test_fts_delete_trigger_cleans_up(db_session):
    """Deleting from emails should remove the matching FTS row via trigger."""
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=201,
        subject="gone soon", content=b"From: s@example.com\nSubject: gone soon\n\nbody",
        timestamp=datetime.datetime(2026, 4, 13),
    )
    db.delete_emails_older_than(datetime.datetime(2099, 1, 1))  # deletes everything

    with db.engine.connect() as conn:
        fts_count = conn.execute(db_text("SELECT COUNT(*) FROM emails_fts")).scalar()
    assert fts_count == 0
```

Add `from sqlalchemy import text as db_text` at the top of the test file if not already present.

- [ ] **Step 1.6: Run the tests**

```bash
poetry run pytest tests/test_database.py -v
```

Expected: 4 new tests PASS (columns, FTS table, save_email populates FTS, delete trigger cleans). Existing database tests still pass.

Full suite:

```bash
poetry run pytest -v
```

Expected: 92 + 4 = 96 tests pass.

- [ ] **Step 1.7: Commit**

```bash
git add database.py reader.py tests/test_database.py
git commit -m "feat: add is_read/is_starred columns + FTS5 virtual table with delete trigger"
```

---

## Task 2: DB helpers — mark_read, mark_starred, filter, search

**Files:**
- Modify: `database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 2.1: Write failing tests for the new helpers**

Append to `tests/test_database.py`:

```python
def test_mark_read_flips_flag(db_session):
    row = insert_email(db_session, email_id=300)
    assert row.is_read is False

    db.mark_read(row.id, True)

    refreshed = db_session.query(db.Email).filter_by(id=row.id).one()
    assert refreshed.is_read is True


def test_mark_read_unflip(db_session):
    row = insert_email(db_session, email_id=301)
    db.mark_read(row.id, True)
    db.mark_read(row.id, False)
    refreshed = db_session.query(db.Email).filter_by(id=row.id).one()
    assert refreshed.is_read is False


def test_mark_starred_flips_flag(db_session):
    row = insert_email(db_session, email_id=302)
    db.mark_starred(row.id, True)
    refreshed = db_session.query(db.Email).filter_by(id=row.id).one()
    assert refreshed.is_starred is True


def test_get_emails_filtered_unread_only(db_session):
    a = insert_email(db_session, email_id=310, sender="s@example.com")
    b = insert_email(db_session, email_id=311, sender="s@example.com")
    db.mark_read(a.id, True)

    rows = db.get_emails_filtered(sender="s@example.com", filter_mode="unread", limit=50)
    assert len(rows) == 1
    assert rows[0]["guid"]  # dict shape with guid field
    # Confirm the returned row is the unread one (email_id=311)
    assert all(r["subject"] for r in rows)


def test_get_emails_filtered_starred_only(db_session):
    a = insert_email(db_session, email_id=320, sender="s@example.com")
    b = insert_email(db_session, email_id=321, sender="s@example.com")
    db.mark_starred(b.id, True)

    rows = db.get_emails_filtered(sender="s@example.com", filter_mode="starred", limit=50)
    assert len(rows) == 1


def test_get_emails_filtered_all_returns_all(db_session):
    insert_email(db_session, email_id=330, sender="s@example.com")
    insert_email(db_session, email_id=331, sender="s@example.com")

    rows = db.get_emails_filtered(sender="s@example.com", filter_mode="all", limit=50)
    assert len(rows) == 2


def test_get_emails_filtered_across_all_senders(db_session):
    insert_email(db_session, email_id=340, sender="alice@example.com")
    insert_email(db_session, email_id=341, sender="bob@example.com")

    rows = db.get_emails_filtered(sender=None, filter_mode="all", limit=50)
    assert len(rows) == 2


def test_get_emails_filtered_rejects_invalid_mode(db_session):
    with pytest.raises(ValueError, match="filter_mode"):
        db.get_emails_filtered(sender=None, filter_mode="garbage", limit=50)


def test_search_emails_finds_match_in_subject(db_session):
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=400,
        subject="Quarterly report released",
        content=b"From: s@example.com\nSubject: Quarterly report released\n\nthe quarterly report is out",
        timestamp=datetime.datetime(2026, 4, 13),
    )

    results = db.search_emails("quarterly", limit=50)
    assert len(results) == 1
    assert "Quarterly report" in results[0]["subject"]


def test_search_emails_finds_match_in_body(db_session):
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=401,
        subject="Newsletter", content=b"From: s@example.com\nSubject: Newsletter\n\nuncommon-term-xyz in body",
        timestamp=datetime.datetime(2026, 4, 13),
    )

    results = db.search_emails("uncommon-term-xyz", limit=50)
    assert len(results) == 1


def test_search_emails_returns_snippet_with_bold_markup(db_session):
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=402,
        subject="Newsletter", content=b"From: s@example.com\nSubject: Newsletter\n\npleasehighlight this word",
        timestamp=datetime.datetime(2026, 4, 13),
    )
    results = db.search_emails("pleasehighlight", limit=50)
    assert len(results) == 1
    assert "<b>" in results[0]["snippet"] and "</b>" in results[0]["snippet"]


def test_search_emails_invalid_syntax_raises_SearchSyntaxError(db_session):
    with pytest.raises(db.SearchSyntaxError):
        db.search_emails("AND AND AND", limit=50)


def test_get_email_by_guid_with_state_returns_read_and_starred(db_session):
    row = insert_email(db_session, email_id=500, sender="sender@example.com")
    db.mark_read(row.id, True)
    db.mark_starred(row.id, True)

    # GUID calculation matches conftest's insert_email defaults
    import hashlib
    guid = hashlib.md5(
        ("Hello from the test suite"
         + "Mon, 13 Apr 2026 10:00:00 +0000"
         + "sender@example.com").encode(),
        usedforsecurity=False,
    ).hexdigest()

    found = db.get_email_by_guid_with_state("sender@example.com", guid)
    assert found is not None
    assert found.is_read is True
    assert found.is_starred is True
```

Add `import pytest` to `tests/test_database.py` if not already present.

- [ ] **Step 2.2: Run the tests (they fail)**

```bash
poetry run pytest tests/test_database.py -v -k "mark_ or get_emails_filtered or search_emails or get_email_by_guid_with_state"
```

Expected: AttributeError / NameError — functions don't exist yet.

- [ ] **Step 2.3: Implement the helpers**

Append to `database.py`:

```python
import sqlite3


class SearchSyntaxError(Exception):
    """Raised when FTS5 MATCH expression is malformed."""


_VALID_FILTER_MODES = {"all", "unread", "starred"}


def mark_read(email_id: int, is_read: bool) -> None:
    """Set the is_read flag on the Email row with primary-key id=email_id."""
    with Session() as session:
        session.query(Email).filter_by(id=email_id).update(
            {Email.is_read: is_read}, synchronize_session=False
        )
        session.commit()


def mark_starred(email_id: int, is_starred: bool) -> None:
    """Set the is_starred flag on the Email row with primary-key id=email_id."""
    with Session() as session:
        session.query(Email).filter_by(id=email_id).update(
            {Email.is_starred: is_starred}, synchronize_session=False
        )
        session.commit()


def get_email_by_guid_with_state(sender: str, guid: str):
    """
    Return the Email row for (sender, guid) including is_read/is_starred, or None.
    Same GUID calculation as get_email_by_guid.
    """
    with Session() as session:
        emails = session.query(Email).filter_by(sender=sender).all()
        for email_record in emails:
            try:
                msg = email.message_from_bytes(email_record.content)
                unique_string = msg["subject"] + msg["date"] + msg["from"]
                calculated_guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()
                if calculated_guid == guid:
                    # Force-load the columns before the session closes
                    _ = email_record.is_read, email_record.is_starred
                    session.expunge(email_record)
                    return email_record
            except Exception:
                logging.debug("Skipping unparseable email id=%s", email_record.id)
                continue
        return None


def get_emails_filtered(sender: str | None, filter_mode: str, limit: int) -> list[dict]:
    """
    Return metadata dicts for emails matching the filter.

    filter_mode ∈ {"all", "unread", "starred"}. When sender is None, queries across
    all senders.

    Returned dict shape:
        {sender, subject, date, guid, timestamp, is_read, is_starred, feed_name}
    """
    if filter_mode not in _VALID_FILTER_MODES:
        raise ValueError(f"filter_mode must be one of {_VALID_FILTER_MODES}, got {filter_mode!r}")

    with Session() as session:
        q = session.query(Email)
        if sender is not None:
            q = q.filter_by(sender=sender)
        if filter_mode == "unread":
            q = q.filter(Email.is_read == False)  # noqa: E712 — SQLAlchemy idiom
        elif filter_mode == "starred":
            q = q.filter(Email.is_starred == True)  # noqa: E712
        q = q.order_by(Email.timestamp.desc()).limit(limit)

        result = []
        for email_record in q.all():
            try:
                msg = email.message_from_bytes(email_record.content)
                subject = str(email.header.make_header(email.header.decode_header(msg["subject"])))
                unique_string = msg["subject"] + msg["date"] + msg["from"]
                guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()
                sanitized = email_record.sender.replace("@", "_").replace(".", "_")
                result.append({
                    "sender": email_record.sender,
                    "subject": subject,
                    "date": msg["date"],
                    "guid": guid,
                    "timestamp": email_record.timestamp,
                    "is_read": email_record.is_read,
                    "is_starred": email_record.is_starred,
                    "feed_name": sanitized,
                })
            except Exception:
                logging.debug("Skipping unparseable email id=%s", email_record.id)
                continue
        return result


def search_emails(query: str, limit: int = 50) -> list[dict]:
    """
    FTS5 search across subject + body_text.

    Returns a list of metadata dicts with a `snippet` field containing FTS5's
    highlighted excerpt (wrapped in <b>...</b> around matches).

    Raises:
        SearchSyntaxError: on malformed MATCH expressions.
    """
    if not query or not query.strip():
        return []

    sql = text(
        "SELECT emails.id, emails.sender, emails.subject, emails.content, emails.timestamp, "
        "snippet(emails_fts, -1, '<b>', '</b>', '...', 20) AS snip "
        "FROM emails_fts "
        "JOIN emails ON emails.id = emails_fts.rowid "
        "WHERE emails_fts MATCH :q "
        "ORDER BY emails.timestamp DESC "
        "LIMIT :lim"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"q": query, "lim": limit}).fetchall()
    except sqlite3.OperationalError as e:
        raise SearchSyntaxError(str(e)) from e

    result = []
    for row_id, sender, subject_raw, content, timestamp, snip in rows:
        try:
            msg = email.message_from_bytes(content)
            subject = str(email.header.make_header(email.header.decode_header(msg["subject"])))
            unique_string = msg["subject"] + msg["date"] + msg["from"]
            guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()
            sanitized = sender.replace("@", "_").replace(".", "_")
            result.append({
                "sender": sender,
                "subject": subject,
                "date": msg["date"],
                "guid": guid,
                "timestamp": timestamp,
                "snippet": snip,
                "feed_name": sanitized,
            })
        except Exception:
            logging.debug("Skipping unparseable search result id=%s", row_id)
            continue
    return result
```

- [ ] **Step 2.4: Run the tests**

```bash
poetry run pytest tests/test_database.py -v
```

Expected: all new tests pass. Total: 96 + 13 = 109 tests.

- [ ] **Step 2.5: Commit**

```bash
git add database.py tests/test_database.py
git commit -m "feat: add mark_read/mark_starred, get_emails_filtered, search_emails helpers"
```

---

## Task 3: Read/star routes + Origin-header CSRF check

**Files:**
- Modify: `common.py`
- Modify: `feed_server.py`
- Modify: `tests/test_feed_server.py`

- [ ] **Step 3.1: Add `read_after_seconds` to config**

In `common.py`, add to the `config` dict (anywhere in the literal):

```python
"read_after_seconds": int(os.getenv("read_after_seconds", "5")),
```

- [ ] **Step 3.2: Write failing tests for read/star routes + Origin check**

Append to `tests/test_feed_server.py`:

```python
def _guid_for_default_fixture():
    """Compute the GUID for insert_email's default values."""
    import hashlib
    return hashlib.md5(
        ("Hello from the test suite"
         + "Mon, 13 Apr 2026 10:00:00 +0000"
         + "sender@example.com").encode(),
        usedforsecurity=False,
    ).hexdigest()


def test_mark_read_route_sets_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    resp = client.post(f"/article/sender_example_com/{guid}/read")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_read": True}

    refreshed = db_session.query(feed_server.db.Email).filter_by(id=row.id).one()
    assert refreshed.is_read is True


def test_unmark_read_route_clears_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    feed_server.db.mark_read(row.id, True)
    guid = _guid_for_default_fixture()

    resp = client.delete(f"/article/sender_example_com/{guid}/read")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_read": False}


def test_star_route_sets_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    resp = client.post(f"/article/sender_example_com/{guid}/star")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_starred": True}


def test_unstar_route_clears_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    feed_server.db.mark_starred(row.id, True)
    guid = _guid_for_default_fixture()

    resp = client.delete(f"/article/sender_example_com/{guid}/star")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_starred": False}


def test_mark_read_route_404s_for_unknown_guid(client, db_session):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    resp = client.post("/article/sender_example_com/nonexistentguid123/read")
    assert resp.status_code == 404


def test_star_route_rejects_cross_origin(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")

    resp = client.post(
        f"/article/sender_example_com/{guid}/star",
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_star_route_allows_missing_origin(client, db_session):
    """No Origin header (e.g. curl) is allowed through."""
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    resp = client.post(f"/article/sender_example_com/{guid}/star")
    # No Origin header by default from Flask test client
    assert resp.status_code == 200
```

- [ ] **Step 3.3: Run tests (they fail)**

```bash
poetry run pytest tests/test_feed_server.py -v -k "mark_read_route or star_route or unstar_route or unmark_read_route"
```

Expected: 404s on every route (they don't exist yet).

- [ ] **Step 3.4: Add the Origin-header helper + routes**

In `feed_server.py`, add an import at top:

```python
from werkzeug.exceptions import HTTPException
# request and abort are already imported
```

Add this helper INSIDE `create_app()` before the route declarations:

```python
    def _assert_same_origin():
        origin = request.headers.get("Origin")
        if not origin:
            return  # absent means non-browser caller (curl, test client) — allow
        baseurl = (config.get("server_baseurl") or "").rstrip("/")
        if baseurl and origin != baseurl:
            abort(403)
```

Add the four state-mutation routes inside `create_app()`:

```python
    @app.post("/article/<feed_name>/<guid>/read")
    def mark_article_read(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_read(record.id, True)
        return jsonify({"is_read": True})

    @app.delete("/article/<feed_name>/<guid>/read")
    def unmark_article_read(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_read(record.id, False)
        return jsonify({"is_read": False})

    @app.post("/article/<feed_name>/<guid>/star")
    def mark_article_starred(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_starred(record.id, True)
        return jsonify({"is_starred": True})

    @app.delete("/article/<feed_name>/<guid>/star")
    def unmark_article_starred(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_starred(record.id, False)
        return jsonify({"is_starred": False})
```

- [ ] **Step 3.5: Run tests (they pass)**

```bash
poetry run pytest tests/test_feed_server.py -v
```

Expected: new tests pass.

Full suite:
```bash
poetry run pytest -v
```

Expected: 109 + 7 = 116 tests.

- [ ] **Step 3.6: Commit**

```bash
git add common.py feed_server.py tests/test_feed_server.py
git commit -m "feat: POST/DELETE routes for read/star state with Origin-header CSRF check"
```

---

## Task 4: Filter param on list routes + `/search` route

**Files:**
- Modify: `feed_server.py`
- Modify: `tests/test_feed_server.py`

- [ ] **Step 4.1: Write failing tests for filter param + search route**

Append to `tests/test_feed_server.py`:

```python
def test_article_list_filter_unread(client, db_session):
    from tests.conftest import insert_email
    a = insert_email(db_session, email_id=1, sender="s@example.com")
    b = insert_email(db_session, email_id=2, sender="s@example.com")
    feed_server.db.mark_read(a.id, True)  # a is read

    resp = client.get("/article?filter=unread")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Unread list should contain b but not a — both have same subject in fixture,
    # distinguish by unread styling via the class
    assert body.count('class="unread"') == 1  # one row with unread class


def test_article_list_filter_starred(client, db_session):
    from tests.conftest import insert_email
    a = insert_email(db_session, email_id=1, sender="s@example.com")
    b = insert_email(db_session, email_id=2, sender="s@example.com")
    feed_server.db.mark_starred(b.id, True)

    resp = client.get("/article?filter=starred")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Only one row should appear (the starred one)
    assert body.count("<li") == 1


def test_article_list_default_filter_is_all(client, db_session):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    insert_email(db_session, email_id=2)

    resp = client.get("/article")  # no filter param
    body = resp.data.decode("utf-8")
    # Both rows present
    assert body.count("<li") == 2


def test_search_route_returns_results(client, db_session):
    feed_server.db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=100,
        subject="ThirdQuarterFinancials",
        content=b"From: s@example.com\nSubject: ThirdQuarterFinancials\n\nreport body",
        timestamp=__import__("datetime").datetime(2026, 4, 13),
    )

    resp = client.get("/search?q=ThirdQuarterFinancials")
    assert resp.status_code == 200
    assert b"ThirdQuarterFinancials" in resp.data


def test_search_route_empty_query_renders_prompt(client, db_session):
    resp = client.get("/search")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Enter a query" in body or "search box above" in body


def test_search_route_invalid_query_renders_error(client, db_session):
    resp = client.get("/search?q=AND%20AND%20AND")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Search error" in body or "error" in body.lower()
```

- [ ] **Step 4.2: Run tests (they fail — routes don't exist yet)**

```bash
poetry run pytest tests/test_feed_server.py -v -k "filter or search_route"
```

Expected: failures (routes missing).

- [ ] **Step 4.3: Extend existing list routes + add `/search` route**

In `feed_server.py`, **replace** the current `article_list` route and `feed_article_list` route bodies with filter-aware versions:

```python
    @app.get("/article")
    def article_list():
        filter_mode = request.args.get("filter", "all")
        if filter_mode not in ("all", "unread", "starred"):
            abort(400)
        articles = db.get_emails_filtered(
            sender=None, filter_mode=filter_mode, limit=config.get("max_item_per_feed", 100) * 10
        )
        # Group by sender for the sidebar
        senders: dict = {}
        for article in articles:
            s = article["sender"]
            if s not in senders:
                senders[s] = {"count": 0, "latest": article["timestamp"], "feed_name": article["feed_name"]}
            senders[s]["count"] += 1
            if article["timestamp"] > senders[s]["latest"]:
                senders[s]["latest"] = article["timestamp"]
        sorted_senders = sorted(senders.items(), key=lambda x: x[1]["latest"], reverse=True)

        return render_template(
            "article_list.html",
            page_title="All Articles",
            articles=articles,
            senders=sorted_senders,
            specific_sender=None,
            filter_mode=filter_mode,
        )

    @app.get("/article/<feed_name>")
    def feed_article_list(feed_name):
        sender_email = feed_name_to_email(feed_name)
        filter_mode = request.args.get("filter", "all")
        if filter_mode not in ("all", "unread", "starred"):
            abort(400)
        articles = db.get_emails_filtered(
            sender=sender_email, filter_mode=filter_mode,
            limit=config.get("max_item_per_feed", 100),
        )
        if not articles and filter_mode == "all":
            abort(404)
        return render_template(
            "article_list.html",
            page_title=f"Articles from {sender_email}",
            articles=articles,
            senders=None,
            specific_sender=sender_email,
            filter_mode=filter_mode,
        )
```

Add the `/search` route:

```python
    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        error = None
        results = []
        if query:
            try:
                results = db.search_emails(query, limit=50)
            except db.SearchSyntaxError as e:
                error = str(e)
        return render_template(
            "search_results.html",
            query=query,
            results=results,
            error=error,
            search_q=query,
        )
```

Update `view_article` to pass read/starred + read_after_seconds:

Find the existing `view_article` function body and modify the `return render_template(...)` call. Replace the current block that computes `body_html, cid_map = reader.extract_body_and_cid_map(msg)` and onwards with:

```python
            body_html, cid_map = reader.extract_body_and_cid_map(msg)

            proxy_base = (config.get("server_baseurl") or "").rstrip("/")
            proxy_origin = proxy_base
            secret = get_img_proxy_secret()

            def _sign(url):
                return img_proxy.sign_url(url, secret, proxy_base)

            cleaned = reader.clean_and_rewrite(body_html, cid_map, _sign)
            iframe_document = reader.render_iframe_document(cleaned, proxy_origin)

            # Fetch state (is_read, is_starred) for the template
            state_record = db.get_email_by_guid_with_state(sender_email, guid)
            is_read = state_record.is_read if state_record else False
            is_starred = state_record.is_starred if state_record else False

            return render_template(
                "article.html",
                subject=subject,
                sender=sender_email,
                date=msg["date"] or "",
                iframe_document=iframe_document,
                feed_name=feed_name,
                guid=guid,
                is_read=is_read,
                is_starred=is_starred,
                read_after_seconds=config.get("read_after_seconds", 5),
            )
```

- [ ] **Step 4.4: Run tests**

```bash
poetry run pytest tests/test_feed_server.py -v
```

Expected: new tests pass. Some existing tests may need adjustments if the new template context vars cause KeyError — check and fix.

Full suite:
```bash
poetry run pytest -v
```

Expected: 116 + 6 = 122 tests (approximate).

- [ ] **Step 4.5: Commit**

```bash
git add feed_server.py tests/test_feed_server.py
git commit -m "feat: filter param on article lists + /search route + view_article state context"
```

---

## Task 5: Templates

**Files:**
- Modify: `templates/base.html`
- Modify: `templates/article_list.html`
- Modify: `templates/article.html`
- Create: `templates/search_results.html`

- [ ] **Step 5.1: Update `base.html`**

Replace the current content of `templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}email2rss{% endblock %}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='reader.css') }}">
  {% block head %}{% endblock %}
</head>
<body>
  <nav class="site-nav">
    <a href="/article" class="nav-home">All Articles</a>
    <form class="nav-search" action="/search" method="get">
      <input type="search" name="q" placeholder="Search..." value="{{ search_q or '' }}" required>
    </form>
  </nav>
  {% block body %}{% endblock %}
</body>
</html>
```

- [ ] **Step 5.2: Update `article_list.html`**

Read the current file first, then replace the body block with the filter-aware version. The full replacement:

```html
{% extends "base.html" %}
{% block title %}{{ page_title }}{% endblock %}
{% block body %}
<h1>{{ page_title }}</h1>

{% set filter_base = '/article/' ~ (articles[0].feed_name if specific_sender and articles else '') %}
<nav class="filter-chips">
  <a href="{% if specific_sender %}/article/{{ articles[0].feed_name if articles }}{% else %}/article{% endif %}?filter=all"
     class="chip {% if filter_mode == 'all' %}active{% endif %}">All</a>
  <a href="{% if specific_sender %}/article/{{ articles[0].feed_name if articles }}{% else %}/article{% endif %}?filter=unread"
     class="chip {% if filter_mode == 'unread' %}active{% endif %}">Unread</a>
  <a href="{% if specific_sender %}/article/{{ articles[0].feed_name if articles }}{% else %}/article{% endif %}?filter=starred"
     class="chip {% if filter_mode == 'starred' %}active{% endif %}">Starred</a>
</nav>

<ul class="article-list">
  {% for article in articles %}
    <li class="{% if not article.is_read %}unread{% endif %}">
      {% if article.is_starred %}<span class="star">★</span>{% endif %}
      <a href="/article/{{ article.feed_name }}/{{ article.guid }}">{{ article.subject }}</a>
      <span class="meta">{{ article.sender }} · {{ article.date }}</span>
    </li>
  {% else %}
    <li class="empty">No articles match this filter.</li>
  {% endfor %}
</ul>

{% if senders %}
  <aside class="senders">
    <h2>Senders</h2>
    <ul>
      {% for sender_email, info in senders %}
        <li><a href="/article/{{ info.feed_name }}">{{ sender_email }}</a> <span class="count">({{ info.count }})</span></li>
      {% endfor %}
    </ul>
  </aside>
{% endif %}
{% endblock %}
```

Note on the filter-base link computation: if `specific_sender` is set, the chips should link back to the same sender page with the filter. If not, they link to `/article`. The simplified version above handles both; if the linter or test reveals issues with the inline Jinja, extract a `{% set base_path = ... %}` at top.

- [ ] **Step 5.3: Update `article.html`**

Replace the current content of `templates/article.html`:

```html
{% extends "base.html" %}
{% block title %}{{ subject }}{% endblock %}
{% block body %}
<article class="article"
         data-feed="{{ feed_name }}"
         data-guid="{{ guid }}"
         data-read-after-seconds="{{ read_after_seconds }}"
         data-is-read="{{ 'true' if is_read else 'false' }}"
         data-is-starred="{{ 'true' if is_starred else 'false' }}">
  <header>
    <h1>{{ subject }}</h1>
    <p class="meta">From: {{ sender }} | Date: {{ date }}</p>
    <div class="article-actions">
      <button id="star-btn" aria-label="Toggle star" type="button">
        <span class="star-icon">{{ '★' if is_starred else '☆' }}</span>
      </button>
      <button id="unread-btn" aria-label="Mark as unread" type="button">Mark unread</button>
    </div>
  </header>
  <iframe
    id="email-body"
    class="email-body-iframe"
    sandbox="allow-popups allow-popups-to-escape-sandbox"
    srcdoc="{{ iframe_document|e }}"
    referrerpolicy="no-referrer"
    loading="lazy"
    title="Email body"
  ></iframe>
</article>
<script src="{{ url_for('static', filename='reader.js') }}"></script>
{% endblock %}
```

The `<script>` at the end wires up the JS. The outer CSP (`default-src 'self'; script-src not specified but default-src applies`) allows same-origin scripts.

- [ ] **Step 5.4: Create `search_results.html`**

Create `templates/search_results.html`:

```html
{% extends "base.html" %}
{% block title %}Search: {{ query }}{% endblock %}
{% block body %}
<h1>Search results</h1>
{% if error %}
  <p class="error">Search error: {{ error }}</p>
{% elif not query %}
  <p>Enter a query in the search box above to search across emails.</p>
{% elif not results %}
  <p>No results for "{{ query }}".</p>
{% else %}
  <p>{{ results|length }} result{% if results|length != 1 %}s{% endif %} for "{{ query }}":</p>
  <ul class="search-results">
    {% for r in results %}
      <li>
        <a href="/article/{{ r.feed_name }}/{{ r.guid }}">{{ r.subject }}</a>
        <span class="meta">{{ r.sender }} · {{ r.date }}</span>
        <p class="snippet">{{ r.snippet|safe }}</p>
      </li>
    {% endfor %}
  </ul>
{% endif %}
{% endblock %}
```

- [ ] **Step 5.5: Run tests**

```bash
poetry run pytest -v
```

Expected: all tests pass. Template tests (filter count, search results presence) should now succeed.

- [ ] **Step 5.6: Commit**

```bash
git add templates/base.html templates/article_list.html templates/article.html templates/search_results.html
git commit -m "feat: templates for filter chips, article toolbar, search results, site nav"
```

---

## Task 6: JS + CSS

**Files:**
- Modify: `static/reader.js`
- Modify: `static/reader.css`

- [ ] **Step 6.1: Rewrite `static/reader.js`**

Replace the entire content of `static/reader.js`:

```js
// Reader UI: dwell-timer auto-read, star toggle, mark-unread button.
// Runs on the outer article page (not inside the sandboxed iframe, where scripts are blocked).

document.addEventListener('DOMContentLoaded', () => {
  const article = document.querySelector('.article[data-feed]');
  if (!article) return;

  const feed = article.dataset.feed;
  const guid = article.dataset.guid;
  const readAfter = Number(article.dataset.readAfterSeconds) || 5;
  const isReadInitially = article.dataset.isRead === 'true';

  // Dwell timer — fire once if not already read
  if (!isReadInitially) {
    setTimeout(() => {
      fetch(`/article/${feed}/${guid}/read`, {
        method: 'POST',
        credentials: 'same-origin',
      }).catch((err) => console.warn('mark-read failed', err));
    }, readAfter * 1000);
  }

  // Star toggle
  const starBtn = document.getElementById('star-btn');
  if (starBtn) {
    starBtn.addEventListener('click', async () => {
      const currentlyStarred = article.dataset.isStarred === 'true';
      const method = currentlyStarred ? 'DELETE' : 'POST';
      try {
        const resp = await fetch(`/article/${feed}/${guid}/star`, {
          method,
          credentials: 'same-origin',
        });
        if (resp.ok) {
          const data = await resp.json();
          article.dataset.isStarred = data.is_starred ? 'true' : 'false';
          const icon = starBtn.querySelector('.star-icon');
          if (icon) icon.textContent = data.is_starred ? '★' : '☆';
        }
      } catch (err) {
        console.warn('star toggle failed', err);
      }
    });
  }

  // Mark-unread — fires DELETE then redirects back to the article list
  const unreadBtn = document.getElementById('unread-btn');
  if (unreadBtn) {
    unreadBtn.addEventListener('click', async () => {
      try {
        await fetch(`/article/${feed}/${guid}/read`, {
          method: 'DELETE',
          credentials: 'same-origin',
        });
      } catch (err) {
        console.warn('mark-unread failed', err);
      }
      window.location.href = '/article';
    });
  }
});
```

- [ ] **Step 6.2: Append to `static/reader.css`**

Append to `static/reader.css`:

```css
/* Site nav (header) */
.site-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #e0e0e0;
  background: #fafafa;
}
.site-nav .nav-home { font-weight: 600; text-decoration: none; color: #222; }
.site-nav .nav-search input[type="search"] {
  padding: 0.4rem 0.6rem;
  border: 1px solid #ccc;
  border-radius: 4px;
  min-width: 250px;
  font-size: 0.9rem;
}

/* Filter chips */
.filter-chips { display: flex; gap: 0.5rem; margin: 1rem 0; }
.filter-chips .chip {
  padding: 0.25rem 0.75rem;
  border: 1px solid #ccc;
  border-radius: 999px;
  text-decoration: none;
  color: #444;
  font-size: 0.85rem;
}
.filter-chips .chip.active { background: #0066cc; color: white; border-color: #0066cc; }

/* Article list unread styling */
.article-list li.unread a { font-weight: 700; }
.article-list .star { color: #d4a017; margin-right: 0.25rem; }

/* Article page toolbar */
.article-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.5rem;
}
.article-actions button {
  padding: 0.3rem 0.7rem;
  border: 1px solid #ccc;
  background: white;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.9rem;
}
.article-actions button:hover { background: #f0f0f0; }
.article-actions #star-btn .star-icon { font-size: 1.1rem; }

/* Search results */
.search-results .snippet {
  color: #555;
  font-size: 0.9rem;
  margin-top: 0.25rem;
}
.search-results .snippet b { background: #fff3a3; font-weight: 600; }

/* Dark mode tweaks for new elements */
@media (prefers-color-scheme: dark) {
  .site-nav { background: #222; border-bottom-color: #444; }
  .site-nav .nav-home { color: #eee; }
  .site-nav .nav-search input[type="search"] {
    background: #333; border-color: #555; color: #eee;
  }
  .filter-chips .chip { border-color: #555; color: #ddd; }
  .filter-chips .chip.active { background: #4a9eff; border-color: #4a9eff; }
  .article-list .star { color: #ffcc44; }
  .article-actions button { background: #333; border-color: #555; color: #eee; }
  .article-actions button:hover { background: #444; }
  .search-results .snippet { color: #aaa; }
  .search-results .snippet b { background: #665500; color: #fff; }
}
```

- [ ] **Step 6.3: Run full suite + lint**

```bash
poetry run pytest -v
poetry run ruff check .
```

Expected: all green.

- [ ] **Step 6.4: Commit**

```bash
git add static/reader.js static/reader.css
git commit -m "feat: reader.js handlers + CSS for nav/filter-chips/star/unread/search"
```

---

## Task 7: Smoke + README

**Files:**
- Modify: `README.md`

- [ ] **Step 7.1: Full suite smoke**

```bash
poetry run pytest -v
```

Expected: ~120+ tests pass.

- [ ] **Step 7.2: Docker build**

```bash
docker build -f Dockerfile.serve -t email2rss-serve:sp4 .
docker build -f Dockerfile.fetch_and_generate -t email2rss-fetch:sp4 .
```

Expected: both succeed. If docker unavailable, note DONE_WITH_CONCERNS.

- [ ] **Step 7.3: Update README**

In `README.md`, add `read_after_seconds` to the Configuration Options table:

```markdown
| `read_after_seconds` | Dwell time (seconds) before an article auto-marks as read | `5` |
```

Add a new "Reader UI features" subsection under "Internal RSS Reader" (before "### Reliability"):

```markdown
### Reader UI Features

- **Read/unread state** — each article auto-marks read after `read_after_seconds` (default 5s) of dwell. Click "Mark unread" in the article toolbar to undo.
- **Starred articles** — click the star icon in the article header to pin an article. Starred articles appear under the "Starred" filter.
- **Filter chips** — article lists have "All / Unread / Starred" chips at the top (URL: `/article?filter=unread`).
- **Full-text search** — the search box in the header queries all stored emails by subject + body via SQLite FTS5. Results highlight the matched term. Supports FTS5's native syntax (phrases in quotes, `AND`/`OR`, prefix `word*`).
```

- [ ] **Step 7.4: Commit**

```bash
git add README.md
git commit -m "docs: document read_after_seconds config and reader UI features"
```

---

## Acceptance criteria checklist

- [ ] `poetry run pytest -v` — ~120+ tests pass
- [ ] `poetry run ruff check .` — clean
- [ ] Docker images build
- [ ] Manual: open `/article`, see filter chips and bold-unread styling
- [ ] Manual: open an article, wait 5s, back to `/article` — that row is no longer bold
- [ ] Manual: click star, reload `/article?filter=starred` — article appears
- [ ] Manual: click "Mark unread" — redirects to `/article`, row is bold again
- [ ] Manual: search for a common word from a newsletter body → results page with highlighted snippet
- [ ] Manual: `/search?q=AND` → friendly error in HTML (not 500)
- [ ] Manual: `curl -X POST http://localhost:8000/article/x/y/read -H "Origin: https://evil.example"` → 403
- [ ] First run with pre-existing `emails.db` → migration adds columns + FTS + backfills without data loss (check logs for "FTS backfill complete")
