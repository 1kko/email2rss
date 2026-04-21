"""Tests for database.py — upsert, queries, indexes."""
import datetime

from sqlalchemy import text
from sqlalchemy import text as db_text

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

    rows = db.get_email("sender@example.com")
    assert isinstance(rows, list)  # materialized; callers can call len()/iterate freely
    timestamps = [r.timestamp for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_email_respects_max_item_per_feed(db_session, monkeypatch):
    # Temporarily lower the limit so we don't need to insert 101 rows
    monkeypatch.setitem(db.config, "max_item_per_feed", 2)
    for i in range(5):
        insert_email(db_session, email_id=i, timestamp=datetime.datetime(2026, 4, 10 + i))

    rows = db.get_email("sender@example.com")
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


def test_delete_emails_older_than_deletes_matching_rows(db_session):
    # 5 rows: days 10, 11, 12, 13, 14 of 2026-04
    for i, day in enumerate([10, 11, 12, 13, 14]):
        insert_email(db_session, email_id=i, timestamp=datetime.datetime(2026, 4, day))

    cutoff = datetime.datetime(2026, 4, 12)
    deleted = db.delete_emails_older_than(cutoff)

    assert deleted == 2
    remaining = db_session.query(db.Email).all()
    assert len(remaining) == 3
    for row in remaining:
        assert row.timestamp >= cutoff


def test_delete_emails_older_than_returns_count(db_session):
    insert_email(db_session, email_id=1, timestamp=datetime.datetime(2026, 4, 1))
    insert_email(db_session, email_id=2, timestamp=datetime.datetime(2026, 4, 2))
    insert_email(db_session, email_id=3, timestamp=datetime.datetime(2026, 4, 20))

    deleted = db.delete_emails_older_than(datetime.datetime(2026, 4, 10))
    assert deleted == 2


def test_delete_emails_older_than_zero_rows_is_noop(db_session):
    insert_email(db_session, email_id=1, timestamp=datetime.datetime(2026, 4, 20))

    deleted = db.delete_emails_older_than(datetime.datetime(2026, 1, 1))
    assert deleted == 0
    assert db.get_entry_count() == 1


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
    content = (
        b"From: s@example.com\nSubject: Indexable subject\n"
        b"Date: Mon, 13 Apr 2026 10:00:00 +0000\n\nlookup me please"
    )
    db.save_email(
        sender="s@example.com",
        receiver="me@localhost",
        email_id=200,
        subject="Indexable subject",
        content=content,
        timestamp=datetime.datetime(2026, 4, 13),
    )
    fts_sql = (
        "SELECT subject, body_text FROM emails_fts"
        " WHERE rowid = (SELECT id FROM emails WHERE email_id = 200)"
    )
    with db.engine.connect() as conn:
        row = conn.execute(db_text(fts_sql)).fetchone()
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
