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
