"""
Database module for the application.
"""

import datetime
import email
import hashlib
import html as _html_escape
import sqlite3

from sqlalchemy import create_engine, event, Column, Integer, String, Text, DateTime, BLOB, Index, Boolean, text
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from common import config, logging

Base = declarative_base()


class Email(Base):
    """
    Represents an email entity.

    Attributes:
        id (int): The unique identifier of the email.
        sender (str): The sender of the email.
        subject (str): The subject of the email.
        content (str): The content of the email.
        timestamp (datetime): The timestamp when the email was received.
    """

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
    preview_image_url = Column(String, nullable=True)  # extracted hero image; None if no usable image

    __table_args__ = (
        Index('idx_sender_timestamp', 'sender', 'timestamp'),
    )


data_dir = config.get("data_dir", "data")
engine = create_engine(f"sqlite:///{data_dir}/emails.db", poolclass=NullPool)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


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
        if "preview_image_url" not in existing_cols:
            logging.info("Adding column: preview_image_url")
            conn.execute(text(
                "ALTER TABLE emails ADD COLUMN preview_image_url TEXT"
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

        # FTS5 virtual table + delete trigger
        _setup_fts(conn)

        conn.commit()

        # Backfill FTS if table is empty but main table has rows (one-time on upgrade)
        fts_count = conn.execute(text("SELECT COUNT(*) FROM emails_fts")).scalar()
        main_count = conn.execute(text("SELECT COUNT(*) FROM emails")).scalar()
        if fts_count == 0 and main_count > 0:
            logging.info(f"Backfilling FTS index for {main_count} existing emails...")
            _backfill_fts_index(conn)

        # Backfill preview_image_url for rows added before the column existed
        missing_preview = conn.execute(
            text("SELECT COUNT(*) FROM emails WHERE preview_image_url IS NULL")
        ).scalar()
        if missing_preview and main_count > 0:
            logging.info(f"Backfilling preview_image_url for {missing_preview} rows...")
            _backfill_preview_images(conn)

        logging.info("Database migration completed successfully")


def _setup_fts(conn):
    """
    Create the FTS5 virtual table and after-delete trigger if they don't exist.
    Safe to call on any connection (including in-memory test DBs).
    """
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
    trigger_exists = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='emails_after_delete'"
    )).fetchone() is not None
    if not trigger_exists:
        logging.info("Creating trigger: emails_after_delete")
        conn.execute(text(
            "CREATE TRIGGER emails_after_delete AFTER DELETE ON emails "
            "BEGIN DELETE FROM emails_fts WHERE rowid = old.id; END"
        ))


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
            {"id": row_id, "s": _html_escape.escape(subject or ""), "b": body_text},
        )
    conn.commit()
    logging.info(f"FTS backfill complete: {len(rows)} rows indexed")


def _backfill_preview_images(conn):
    """Populate preview_image_url for emails that predate the column. One-time."""
    import reader  # local import to avoid circular dep

    rows = conn.execute(
        text("SELECT id, content FROM emails WHERE preview_image_url IS NULL")
    ).fetchall()
    for row_id, content in rows:
        try:
            msg = email.message_from_bytes(content)
            preview = reader.extract_preview_image(msg)
        except Exception:
            preview = None
            logging.warning(f"preview backfill: extraction failed for id={row_id}")
        conn.execute(
            text("UPDATE emails SET preview_image_url = :p WHERE id = :id"),
            # "" sentinel = inspected, no usable image (prevents re-parsing on next startup)
            {"p": preview or "", "id": row_id},
        )
    conn.commit()
    logging.info(f"preview_image_url backfill complete: {len(rows)} rows processed")


# Run migration on startup
migrate_database()

Session = sessionmaker(bind=engine)


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
            # After commit we know new_email.id — write matching FTS row + preview URL
            try:
                msg = email.message_from_bytes(content)
                body_text = reader.extract_plain_text(msg)
                preview = reader.extract_preview_image(msg)
            except Exception:
                body_text = ""
                preview = None
                logging.warning(f"save_email: extraction failed for email_id={email_id}")
            session.execute(
                text("INSERT INTO emails_fts(rowid, subject, body_text) VALUES (:id, :s, :b)"),
                {"id": new_email.id, "s": _html_escape.escape(subject or ""), "b": body_text},
            )
            # Always write a value — use "" as "inspected, no image" sentinel so
            # _backfill_preview_images can skip rows we've already checked.
            session.execute(
                text("UPDATE emails SET preview_image_url = :p WHERE id = :id"),
                {"p": preview or "", "id": new_email.id},
            )
            session.commit()
        else:
            print(f"Email with id {email_id} already exists. Discarding.")


def get_email(sender: str) -> list:
    """
    Get all emails from a specific sender.

    Args:
        sender (str): The sender of the emails.

    Returns:
        list: A list of email objects.
    """

    max_item_per_feed = config.get("max_item_per_feed")

    with Session() as session:
        return (
            session.query(Email)
            .filter_by(sender=sender)
            .order_by(Email.timestamp.desc())
            .limit(max_item_per_feed)
            .all()
        )


def get_senders() -> list:
    """
    Get all unique senders from the database.

    Returns:
        list: A list of unique sender email addresses.
    """

    with Session() as session:
        senders = session.query(Email.sender).distinct().all()
        return [sender[0] for sender in senders]


def get_entry_count():
    """
    Check if the database is empty.

    Returns:
        bool: True if the database is empty, False otherwise.
    """

    with Session() as session:
        return session.query(Email).count()


def get_last_email_id():
    """
    Get the last email id from the database.

    Returns:
        int: The last email id.
    """

    with Session() as session:
        last_email = session.query(Email).order_by(Email.timestamp.desc()).first()
        if last_email:
            return last_email.email_id
        return 0


def get_email_by_guid(sender: str, guid: str):
    """
    Get an email by its sender and GUID.

    The GUID is calculated as MD5(subject + date + from) from the email message.
    This function queries emails by sender and calculates GUID for each to find a match.

    Args:
        sender (str): The sender email address
        guid (str): The MD5 GUID hash to match

    Returns:
        Email object if found, None otherwise
    """

    with Session() as session:
        emails = session.query(Email).filter_by(sender=sender).all()

        for email_record in emails:
            try:
                # Parse the email content BLOB
                msg = email.message_from_bytes(email_record.content)

                # Calculate GUID using the same logic as feed_generator.py
                unique_string = msg["subject"] + msg["date"] + msg["from"]
                calculated_guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()

                # Check if this is the email we're looking for
                if calculated_guid == guid:
                    return email_record

            except Exception:
                logging.debug("Skipping unparseable email id=%s", email_record.id)
                continue

        return None


def get_all_emails_with_metadata():
    """
    Get all emails from the database with parsed metadata.

    Returns:
        list: A list of dictionaries containing email metadata:
            - sender: sender email address
            - subject: email subject
            - date: email date
            - guid: calculated MD5 GUID
            - timestamp: database timestamp
    """
    max_item_per_feed = config.get("max_item_per_feed")

    with Session() as session:
        emails = (
            session.query(Email)
            .order_by(Email.timestamp.desc())
            .limit(max_item_per_feed * 10)  # Limit to reasonable number
            .all()
        )

        result = []
        for email_record in emails:
            try:
                msg = email.message_from_bytes(email_record.content)
                subject = email.header.make_header(email.header.decode_header(msg["subject"]))
                subject_text = str(subject)
                date_text = msg["date"]

                # Calculate GUID
                unique_string = msg["subject"] + msg["date"] + msg["from"]
                guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()

                result.append({
                    "sender": email_record.sender,
                    "subject": subject_text,
                    "date": date_text,
                    "guid": guid,
                    "timestamp": email_record.timestamp,
                })
            except Exception:
                logging.debug("Skipping unparseable email id=%s", email_record.id)
                continue

        return result


def get_emails_by_sender_with_metadata(sender: str):
    """
    Get all emails from a specific sender with parsed metadata.

    Args:
        sender (str): The sender email address

    Returns:
        list: A list of dictionaries containing email metadata
    """
    max_item_per_feed = config.get("max_item_per_feed")

    with Session() as session:
        emails = (
            session.query(Email)
            .filter_by(sender=sender)
            .order_by(Email.timestamp.desc())
            .limit(max_item_per_feed)
            .all()
        )

        result = []
        for email_record in emails:
            try:
                msg = email.message_from_bytes(email_record.content)
                subject = email.header.make_header(email.header.decode_header(msg["subject"]))
                subject_text = str(subject)
                date_text = msg["date"]

                # Calculate GUID
                unique_string = msg["subject"] + msg["date"] + msg["from"]
                guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()

                result.append({
                    "sender": email_record.sender,
                    "subject": subject_text,
                    "date": date_text,
                    "guid": guid,
                    "timestamp": email_record.timestamp,
                })
            except Exception:
                logging.debug("Skipping unparseable email id=%s", email_record.id)
                continue

        return result


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
                unique_string = (msg["subject"] or "") + (msg["date"] or "") + (msg["from"] or "")
                calculated_guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()
                if calculated_guid == guid:
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

    filter_mode in {"all", "unread", "starred"}. When sender is None, queries
    across all senders.

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
            q = q.filter(Email.is_read == False)  # noqa: E712
        elif filter_mode == "starred":
            q = q.filter(Email.is_starred == True)  # noqa: E712
        q = q.order_by(Email.timestamp.desc()).limit(limit)

        result = []
        for email_record in q.all():
            try:
                msg = email.message_from_bytes(email_record.content)
                subject = str(email.header.make_header(email.header.decode_header(msg["subject"])))
                unique_string = (msg["subject"] or "") + (msg["date"] or "") + (msg["from"] or "")
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
    from sqlalchemy.exc import OperationalError as SAOperationalError

    def _run_query(q):
        with engine.connect() as conn:
            return conn.execute(sql, {"q": q, "lim": limit}).fetchall()

    try:
        rows = _run_query(query)
    except (sqlite3.OperationalError, SAOperationalError) as exc:
        # FTS5 "syntax error" means the query itself is malformed (e.g. AND AND AND).
        # Other OperationalErrors (e.g. "no such column") can arise from FTS5
        # mis-tokenising a user query that contains hyphens or other punctuation;
        # retry as a phrase query so that "foo-bar" finds the literal token sequence.
        raw_msg = str(exc).lower()
        if "syntax error" in raw_msg:
            raise SearchSyntaxError(str(exc)) from exc
        try:
            rows = _run_query(f'"{query}"')
        except (sqlite3.OperationalError, SAOperationalError) as exc2:
            raise SearchSyntaxError(str(exc2)) from exc2

    result = []
    for row_id, sender, subject_raw, content, timestamp, snip in rows:
        try:
            msg = email.message_from_bytes(content)
            subject = str(email.header.make_header(email.header.decode_header(msg["subject"])))
            unique_string = (msg["subject"] or "") + (msg["date"] or "") + (msg["from"] or "")
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


def delete_emails_older_than(cutoff: datetime.datetime) -> int:
    """
    Delete emails with timestamp < cutoff. Returns the number of rows deleted.

    Does NOT run VACUUM afterwards. The project uses WAL mode with a concurrent
    reader process (feed_server); VACUUM would require an exclusive lock that
    conflicts with the reader's open read transaction. SQLite reuses the freed
    pages for subsequent inserts, so the row count stays bounded; the file size
    doesn't shrink but also doesn't grow unbounded. To reclaim on-disk space,
    stop the app and run `sqlite3 emails.db "VACUUM"` by hand.
    """
    with Session() as session:
        deleted = (
            session.query(Email)
            .filter(Email.timestamp < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
    return deleted
