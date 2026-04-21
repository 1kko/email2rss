"""Tests for database.py — upsert, queries, indexes."""
import datetime

import pytest
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
    insert_email(db_session, email_id=311, sender="s@example.com")
    db.mark_read(a.id, True)

    rows = db.get_emails_filtered(sender="s@example.com", filter_mode="unread", limit=50)
    assert len(rows) == 1
    assert rows[0]["guid"]
    assert all(r["subject"] for r in rows)


def test_get_emails_filtered_starred_only(db_session):
    insert_email(db_session, email_id=320, sender="s@example.com")
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


def test_mark_starred_unflip(db_session):
    row = insert_email(db_session, email_id=600)
    db.mark_starred(row.id, True)
    db.mark_starred(row.id, False)
    refreshed = db_session.query(db.Email).filter_by(id=row.id).one()
    assert refreshed.is_starred is False


def test_backfill_fts_populates_existing_rows(db_session):
    """Simulate pre-FTS-migration DB: insert rows via raw SQL bypassing save_email,
    drop the FTS rows that insert_email's save_email path added, then run
    _backfill_fts_index and confirm search finds the rows."""
    from sqlalchemy import text as _text
    import database as dbmod

    # Drop and recreate the FTS table so we start empty
    with dbmod.engine.connect() as conn:
        conn.execute(_text("DROP TABLE emails_fts"))
        conn.commit()
        dbmod._setup_fts(conn)
        conn.commit()

    # Insert an email via save_email (populates both tables)
    dbmod.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=700,
        subject="BackfillTestSubject",
        content=b"From: s@example.com\nSubject: BackfillTestSubject\n\ndistinctive-backfill-word",
        timestamp=datetime.datetime(2026, 4, 13),
    )

    # Manually clear FTS (simulating a pre-migration DB)
    with dbmod.engine.connect() as conn:
        conn.execute(_text("DELETE FROM emails_fts"))
        conn.commit()

    # Confirm search returns nothing now
    assert db.search_emails("distinctive-backfill-word") == []

    # Run backfill
    with dbmod.engine.connect() as conn:
        dbmod._backfill_fts_index(conn)

    # Now search should find the row
    results = db.search_emails("distinctive-backfill-word")
    assert len(results) == 1
    assert "BackfillTest" in results[0]["subject"]


def test_fts_subject_is_html_escaped(db_session):
    """Malicious subject content must be HTML-escaped in the FTS table so that
    snippet() output is safe to render with |safe filter."""
    from sqlalchemy import text as _text

    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=800,
        subject="<img src=x onerror=alert(1)> XssProbeToken",
        content=b"From: s@example.com\nSubject: <img src=x onerror=alert(1)> XssProbeToken\n\nbody",
        timestamp=datetime.datetime(2026, 4, 13),
    )
    with db.engine.connect() as conn:
        stored_subject = conn.execute(
            _text("SELECT subject FROM emails_fts WHERE rowid = (SELECT id FROM emails WHERE email_id = 800)")
        ).scalar()
    # HTML should be escaped in the stored FTS subject
    assert "<img" not in stored_subject
    assert "&lt;img" in stored_subject
    # Search still finds it via the distinctive token
    results = db.search_emails("XssProbeToken")
    assert len(results) == 1


def test_email_model_has_preview_image_url_column(db_session):
    """Fresh in-memory DB should have the new column, default None."""
    insert_email(db_session, email_id=1)
    row = db_session.query(db.Email).filter_by(email_id=1).first()
    assert row.preview_image_url is None


def test_save_email_populates_preview_image_url(db_session):
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<p>body</p><img src="http://cdn.example.com/hero.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=2,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    row = db_session.query(db.Email).filter_by(email_id=2).first()
    assert row.preview_image_url == "http://cdn.example.com/hero.jpg"


def test_save_email_leaves_preview_null_when_no_usable_image(db_session):
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>no images</p>"
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=3,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    row = db_session.query(db.Email).filter_by(email_id=3).first()
    # Empty string sentinel = inspected, no image. Contrast with NULL = never inspected.
    assert row.preview_image_url == ""


def test_backfill_skips_rows_already_inspected_with_empty_sentinel(db_session):
    """Rows with preview_image_url = '' (inspected, no image) should NOT be
    reprocessed on subsequent backfill runs."""
    from sqlalchemy import text as _text

    # Insert a row without an image — save_email writes "" sentinel
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>no images</p>"
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=99,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )

    # Backfill: should find zero NULL rows and do nothing
    with db.engine.connect() as conn:
        null_count_before = conn.execute(
            _text("SELECT COUNT(*) FROM emails WHERE preview_image_url IS NULL")
        ).scalar()
        db._backfill_preview_images(conn)
        null_count_after = conn.execute(
            _text("SELECT COUNT(*) FROM emails WHERE preview_image_url IS NULL")
        ).scalar()

    assert null_count_before == 0
    assert null_count_after == 0
    # Value unchanged: still the inspected-no-image sentinel
    row = db_session.query(db.Email).filter_by(email_id=99).first()
    assert row.preview_image_url == ""


def test_backfill_preview_images_populates_existing_rows(db_session):
    """Simulate a pre-migration DB: insert a row with an image, then clear
    preview_image_url, then run _backfill_preview_images."""
    from sqlalchemy import text as _text

    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<img src="http://x.com/pic.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=4,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    # Reset to simulate pre-column state
    with db.engine.connect() as conn:
        conn.execute(_text("UPDATE emails SET preview_image_url = NULL"))
        conn.commit()
        db._backfill_preview_images(conn)

    row = db_session.query(db.Email).filter_by(email_id=4).first()
    assert row.preview_image_url == "http://x.com/pic.jpg"


def test_migrate_v1_invalidates_and_rebackfills_preview_image_url(db_session):
    """
    v1 migration: pre-v1 rows have preview_image_url values cached before the
    logo-filter + largest-image extractor landed. Running migrate_database()
    on a user_version=0 DB nulls them, then _backfill_preview_images re-extracts
    with current logic. user_version bumps to 1.
    """
    content = (
        b"From: s@example.com\r\nSubject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<img src="http://x.com/real-hero.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=777,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    # Simulate stale cached logo URL + pre-v1 schema version
    with db.engine.connect() as conn:
        conn.execute(text(
            "UPDATE emails SET preview_image_url = 'http://cdn.x.com/stale-logo.png' "
            "WHERE email_id = 777"
        ))
        conn.execute(text("PRAGMA user_version = 0"))
        conn.commit()

    db.migrate_database()

    with db.engine.connect() as conn:
        uv = conn.execute(text("PRAGMA user_version")).scalar()
    assert uv == 1

    db_session.expire_all()
    row = db_session.query(db.Email).filter_by(email_id=777).first()
    assert row.preview_image_url == "http://x.com/real-hero.jpg"


def test_migrate_v1_idempotent_when_user_version_is_1(db_session):
    """Second run of migrate_database must NOT null already-valid preview_image_url."""
    content = b"From: s@example.com\r\nSubject: t\r\n\r\nbody"
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=888,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    # Simulate post-v1 state: set user_version=1 and a specific preview URL
    with db.engine.connect() as conn:
        conn.execute(text(
            "UPDATE emails SET preview_image_url = 'http://keep.me/pic.png' "
            "WHERE email_id = 888"
        ))
        conn.execute(text("PRAGMA user_version = 1"))
        conn.commit()

    db.migrate_database()

    db_session.expire_all()
    row = db_session.query(db.Email).filter_by(email_id=888).first()
    assert row.preview_image_url == "http://keep.me/pic.png"


def test_get_landing_data_empty_db(db_session):
    data = db.get_landing_data(latest_limit=10, per_sender_limit=10)
    assert data == {"latest": [], "rows": []}


def test_get_landing_data_returns_latest_and_rows(db_session):
    # alice: 2 articles. bob: 1 article.
    insert_email(db_session, email_id=1, sender="alice@example.com",
                 timestamp=datetime.datetime(2026, 4, 10))
    insert_email(db_session, email_id=2, sender="alice@example.com",
                 timestamp=datetime.datetime(2026, 4, 15))
    insert_email(db_session, email_id=3, sender="bob@example.com",
                 timestamp=datetime.datetime(2026, 4, 12))

    data = db.get_landing_data(latest_limit=10, per_sender_limit=10)
    assert len(data["latest"]) == 3
    # Latest ordered by timestamp desc
    assert data["latest"][0]["sender"] == "alice@example.com"  # 2026-04-15
    assert data["latest"][1]["sender"] == "bob@example.com"    # 2026-04-12
    assert data["latest"][2]["sender"] == "alice@example.com"  # 2026-04-10

    # Rows ordered by each sender's newest article desc — alice (04-15) before bob (04-12)
    assert [r["sender"] for r in data["rows"]] == ["alice@example.com", "bob@example.com"]
    alice_row = data["rows"][0]
    bob_row = data["rows"][1]
    assert alice_row["article_count"] == 2
    assert bob_row["article_count"] == 1
    # Per-row articles sorted newest-first
    assert len(alice_row["articles"]) == 2
    assert alice_row["articles"][0]["sender"] == "alice@example.com"


def test_get_landing_data_limits_latest(db_session):
    for i in range(15):
        insert_email(db_session, email_id=i,
                     timestamp=datetime.datetime(2026, 4, 10) + datetime.timedelta(hours=i))
    data = db.get_landing_data(latest_limit=5, per_sender_limit=10)
    assert len(data["latest"]) == 5


def test_get_landing_data_limits_per_sender(db_session):
    for i in range(15):
        insert_email(db_session, email_id=i, sender="alice@example.com",
                     timestamp=datetime.datetime(2026, 4, 10) + datetime.timedelta(hours=i))
    data = db.get_landing_data(latest_limit=10, per_sender_limit=5)
    assert data["rows"][0]["article_count"] == 15
    assert len(data["rows"][0]["articles"]) == 5


def test_get_landing_data_includes_favicon_and_monogram(db_session):
    insert_email(db_session, email_id=1, sender="alice@example.com")
    data = db.get_landing_data()
    row = data["rows"][0]
    assert row["favicon_url"]  # non-empty signed URL
    assert row["monogram_letter"] == "A"
    assert 0 <= row["monogram_hue"] < 360


def test_get_landing_data_signs_preview_urls(db_session):
    """preview_image_url values flow through img_proxy.sign_url — each card's
    image_url should start with the /img? prefix, not be the bare remote URL."""
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<img src="http://cdn.example.com/hero.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=1,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    data = db.get_landing_data()
    article = data["latest"][0]
    assert article["image_url"]
    assert "/img?u=" in article["image_url"]
    assert "&sig=" in article["image_url"]
