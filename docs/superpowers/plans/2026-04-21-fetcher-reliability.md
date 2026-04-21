# Fetcher Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the IMAP fetcher tolerant of transient connection failures and per-email parsing errors, and add opt-in database retention so rows don't accumulate forever.

**Architecture:** `email_fetcher.connect_to_gmail` gets an exponential-backoff retry loop (4 attempts, delays 0/1/2/4s). `email_fetcher.fetch_emails` gets a per-message try/except that logs malformed emails and continues instead of aborting the cycle; IMAP-level errors still propagate. `database.delete_emails_older_than(cutoff)` purges old rows when `retention_days` config is truthy. No `VACUUM` (incompatible with WAL + concurrent reader).

**Tech Stack:** Python 3.12, `imaplib` (stdlib), SQLAlchemy 2.0, pytest with `unittest.mock` for IMAP fakes.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `common.py` | modify | Add `retention_days` config entry |
| `database.py` | modify | Add `delete_emails_older_than(cutoff)` |
| `email_fetcher.py` | modify | Retry loop in `connect_to_gmail`; per-email try/except in `fetch_emails`; purge call at top of `main()` |
| `tests/test_database.py` | modify | 3 new tests for retention |
| `tests/test_email_fetcher.py` | **create** | IMAP mocked tests for retry, per-email resilience, retention wiring |

---

## Task 1: `retention_days` config + `delete_emails_older_than`

**Files:**
- Modify: `common.py`
- Modify: `database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1.1: Add `retention_days` to `common.config`**

In `common.py`, find the `config = {...}` literal and add this entry alongside the others (anywhere in the dict):

```python
"retention_days": int(os.getenv("retention_days", "0")) or None,
```

`"0"` → `0` → falsy → becomes `None` via the `or None` trick. Any positive int stays int.

- [ ] **Step 1.2: Write failing tests for `delete_emails_older_than`**

Append to `tests/test_database.py`:

```python
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
```

- [ ] **Step 1.3: Run tests to verify they fail**

```bash
poetry run pytest tests/test_database.py -v -k delete_emails_older_than
```

Expected: `AttributeError: module 'database' has no attribute 'delete_emails_older_than'`.

- [ ] **Step 1.4: Implement `delete_emails_older_than` in `database.py`**

Append to `database.py` (after the existing query helpers, before end of file):

```python
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
```

- [ ] **Step 1.5: Run tests to verify they pass**

```bash
poetry run pytest tests/test_database.py -v -k delete_emails_older_than
```

Expected: 3 tests PASS.

Also run the full suite to confirm no regressions:

```bash
poetry run pytest -v
```

Expected: ~82 tests pass (79 existing + 3 new).

- [ ] **Step 1.6: Commit**

```bash
git add common.py database.py tests/test_database.py
git commit -m "feat: add retention_days config and delete_emails_older_than"
```

---

## Task 2: Connection retry in `connect_to_gmail`

**Files:**
- Modify: `email_fetcher.py`
- Create: `tests/test_email_fetcher.py`

- [ ] **Step 2.1: Write failing tests for connection retry**

Create `tests/test_email_fetcher.py`:

```python
"""Tests for email_fetcher — connection retry, per-email resilience, retention wiring."""
import datetime
import imaplib
from unittest.mock import MagicMock, patch, call

import pytest

import email_fetcher


@pytest.fixture
def fake_imap(monkeypatch):
    """Patch imaplib.IMAP4_SSL so tests don't touch the network."""
    mock_class = MagicMock()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", mock_class)
    return mock_class


def test_connect_to_gmail_succeeds_on_first_attempt(fake_imap, monkeypatch):
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    mail = email_fetcher.connect_to_gmail("imap.test", "user", "pw", "INBOX")
    assert mail is fake_imap.return_value
    assert fake_imap.call_count == 1


def test_connect_to_gmail_retries_on_oserror(fake_imap, monkeypatch):
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    # First 2 constructor calls raise OSError; 3rd returns a working mock
    good_mail = MagicMock()
    fake_imap.side_effect = [OSError("boom"), OSError("still boom"), good_mail]

    mail = email_fetcher.connect_to_gmail("imap.test", "user", "pw", "INBOX")
    assert mail is good_mail
    assert fake_imap.call_count == 3


def test_connect_to_gmail_retries_on_imap_error(fake_imap, monkeypatch):
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    # First mail object's login() fails; second succeeds
    failing_mail = MagicMock()
    failing_mail.login.side_effect = imaplib.IMAP4.error("auth blip")
    good_mail = MagicMock()
    fake_imap.side_effect = [failing_mail, good_mail]

    mail = email_fetcher.connect_to_gmail("imap.test", "user", "pw", "INBOX")
    assert mail is good_mail


def test_connect_to_gmail_raises_after_all_retries_fail(fake_imap, monkeypatch):
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    fake_imap.side_effect = OSError("persistent network failure")

    with pytest.raises(OSError, match="persistent"):
        email_fetcher.connect_to_gmail("imap.test", "user", "pw", "INBOX")
    assert fake_imap.call_count == 4


def test_connect_to_gmail_applies_exponential_backoff(fake_imap, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: sleep_calls.append(s))
    fake_imap.side_effect = OSError("fail forever")

    with pytest.raises(OSError):
        email_fetcher.connect_to_gmail("imap.test", "user", "pw", "INBOX")

    # Expect sleeps of 1, 2, 4 between the 4 attempts (no sleep before the first)
    assert sleep_calls == [1, 2, 4]
```

- [ ] **Step 2.2: Run tests to verify they fail (or pass for the first one)**

```bash
poetry run pytest tests/test_email_fetcher.py -v -k connect_to_gmail
```

Expected: `test_connect_to_gmail_succeeds_on_first_attempt` may pass (current code happy-path works). Retry tests FAIL — current code raises on first exception.

- [ ] **Step 2.3: Implement the retry loop**

In `email_fetcher.py`, replace the body of `connect_to_gmail`:

```python
def connect_to_gmail(imap_server, username, password, mailbox="INBOX"):
    """
    Connects to the IMAP server with exponential backoff on transient failures.

    Retries up to 4 times total with delays [0, 1, 2, 4] seconds between attempts.
    Retries on `imaplib.IMAP4.error` (which covers `imaplib.IMAP4.abort`) and
    `OSError` (network issues). Other exceptions propagate unchanged.

    Returns:
        imaplib.IMAP4_SSL: The connected and mailbox-selected IMAP object.

    Raises:
        imaplib.IMAP4.error | OSError: If all retries fail.
    """
    delays = [0, 1, 2, 4]
    last_err: Exception | None = None
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            mail = imaplib.IMAP4_SSL(imap_server)
            mail.login(username, password)
            mail.select(mailbox)
            logging.info(f"Connected to IMAP and selected {mailbox} (attempt {attempt + 1}).")
            return mail
        except (imaplib.IMAP4.error, OSError) as e:
            last_err = e
            logging.warning(f"IMAP connect attempt {attempt + 1} failed: {e}")
    logging.error(f"IMAP connect failed after {len(delays)} attempts: {last_err}")
    assert last_err is not None  # for the type checker
    raise last_err
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
poetry run pytest tests/test_email_fetcher.py -v -k connect_to_gmail
```

Expected: 5 tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add email_fetcher.py tests/test_email_fetcher.py
git commit -m "feat: exponential-backoff retry in connect_to_gmail"
```

---

## Task 3: Per-email resilience in `fetch_emails`

**Files:**
- Modify: `email_fetcher.py`
- Modify: `tests/test_email_fetcher.py`

- [ ] **Step 3.1: Write failing tests for per-email resilience**

Append to `tests/test_email_fetcher.py`:

```python
def _mime_bytes(subject, sender="s@example.com", date_str="Mon, 13 Apr 2026 10:00:00 +0000"):
    return (
        f"From: {sender}\r\n"
        f"To: user@localhost\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date_str}\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"body for {subject}\r\n"
    ).encode("utf-8")


def test_fetch_emails_continues_past_malformed_email(db_session):
    """Malformed Date header on the middle message — other 2 still saved."""
    mail = MagicMock()
    mail.search.return_value = (None, [b"1 2 3"])
    good_a = _mime_bytes("A", date_str="Mon, 13 Apr 2026 10:00:00 +0000")
    # Mid email has a Date header that parsedate_to_datetime can't parse
    bad = _mime_bytes("B", date_str="not-a-real-date")
    good_c = _mime_bytes("C", date_str="Mon, 15 Apr 2026 10:00:00 +0000")
    mail.fetch.side_effect = [
        (None, [(b"1 (RFC822 {len})", good_a)]),
        (None, [(b"2 (RFC822 {len})", bad)]),
        (None, [(b"3 (RFC822 {len})", good_c)]),
    ]

    email_fetcher.fetch_emails(mail, since=10)

    # 2 of 3 survive (A and C). B's unparseable date trips parsedate_to_datetime.
    import database as db
    assert db.get_entry_count() == 2


def test_fetch_emails_aborts_on_imap_error(db_session):
    """IMAP-level error on message 2 propagates; message 1 is saved, 3 is not."""
    mail = MagicMock()
    mail.search.return_value = (None, [b"1 2 3"])
    good_a = _mime_bytes("A")
    mail.fetch.side_effect = [
        (None, [(b"1 (RFC822 {len})", good_a)]),
        imaplib.IMAP4.error("connection dropped"),
        # 3rd call should never happen
    ]

    with pytest.raises(imaplib.IMAP4.error, match="connection dropped"):
        email_fetcher.fetch_emails(mail, since=10)

    import database as db
    assert db.get_entry_count() == 1  # only A landed
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
poetry run pytest tests/test_email_fetcher.py -v -k fetch_emails
```

Expected: `test_fetch_emails_continues_past_malformed_email` FAILS — current implementation raises on the bad date and skips C. `test_fetch_emails_aborts_on_imap_error` may pass (current code does propagate) but the pre-exception save of A needs verification.

- [ ] **Step 3.3: Rewrite the inner loop of `fetch_emails`**

In `email_fetcher.py`, find the inner `for index, num in enumerate(messages):` loop and replace its body:

```python
        for index, num in enumerate(messages):
            logging.info(f"Processing email {index + 1} of {len(messages)}.")
            try:
                _, data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                sender = extract_email_address(msg["from"], default="unknown@email.com")
                receiver = extract_email_address(msg["to"], default="you@email.com")
                logging.info(
                    f"Email from {sender}. title: {msg['subject']} by {msg['date']}"
                )
                article_date = email.utils.parsedate_to_datetime(msg["date"])

                db.save_email(
                    sender=sender,
                    receiver=receiver,
                    subject=msg["subject"],
                    email_id=int(num),
                    content=data[0][1],
                    timestamp=article_date,
                )
                _emails_received.add(1, {"sender": sender})

                if sender not in emails:
                    emails[sender] = []
                emails[sender].append(msg)
            except (imaplib.IMAP4.error, OSError) as e:
                # IMAP-level problem — the connection is in an unknown state.
                # Abort the cycle; the next cycle will reconnect from scratch.
                logging.error(f"IMAP error while fetching message {num}: {e}")
                raise
            except Exception:
                # Per-email parse/DB error — log with traceback, skip, continue.
                logging.exception(f"Skipping malformed email num={num}")
                continue
```

The outer `try/except Exception` wrapping the whole function (at the current end of `fetch_emails`) can stay — but since per-IMAP errors now re-raise explicitly, the outer wrapper catches only truly unexpected failures. Leave the outer `except Exception: ... raise` as-is to preserve the structured error-metric path.

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
poetry run pytest tests/test_email_fetcher.py -v
```

Expected: all ~7 tests pass so far (5 connect + 2 fetch_emails).

- [ ] **Step 3.5: Commit**

```bash
git add email_fetcher.py tests/test_email_fetcher.py
git commit -m "feat: per-email try/except so one bad message does not kill the cycle"
```

---

## Task 4: Retention wiring in `email_fetcher.main()`

**Files:**
- Modify: `email_fetcher.py`
- Modify: `tests/test_email_fetcher.py`

- [ ] **Step 4.1: Write failing tests for retention wiring**

Append to `tests/test_email_fetcher.py`:

```python
def test_main_calls_purge_when_retention_days_set(monkeypatch, fake_imap, db_session):
    """When retention_days is set, main() calls delete_emails_older_than with
    a cutoff ~retention_days ago before connecting."""
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    monkeypatch.setitem(email_fetcher.config, "retention_days", 7)
    # Stub out IMAP so the fetch side is a no-op
    fake_imap.return_value.search.return_value = (None, [b""])

    import database as db
    delete_calls = []

    def fake_delete(cutoff):
        delete_calls.append(cutoff)
        return 0
    monkeypatch.setattr(db, "delete_emails_older_than", fake_delete)

    email_fetcher.main()

    assert len(delete_calls) == 1
    cutoff = delete_calls[0]
    expected = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    # Cutoff should be within 1 minute of expected
    assert abs((cutoff - expected).total_seconds()) < 60


def test_main_skips_purge_when_retention_days_none(monkeypatch, fake_imap, db_session):
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    monkeypatch.setitem(email_fetcher.config, "retention_days", None)
    fake_imap.return_value.search.return_value = (None, [b""])

    import database as db
    delete_calls = []
    monkeypatch.setattr(db, "delete_emails_older_than",
                        lambda c: delete_calls.append(c) or 0)

    email_fetcher.main()

    assert delete_calls == []


def test_main_skips_purge_when_retention_days_zero(monkeypatch, fake_imap, db_session):
    """retention_days=0 is coerced to None by config; double-check that main() respects it."""
    monkeypatch.setattr(email_fetcher.time, "sleep", lambda s: None)
    monkeypatch.setitem(email_fetcher.config, "retention_days", 0)
    fake_imap.return_value.search.return_value = (None, [b""])

    import database as db
    delete_calls = []
    monkeypatch.setattr(db, "delete_emails_older_than",
                        lambda c: delete_calls.append(c) or 0)

    email_fetcher.main()

    assert delete_calls == []
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
poetry run pytest tests/test_email_fetcher.py -v -k "main_"
```

Expected: all 3 tests FAIL — main() currently doesn't call delete_emails_older_than.

- [ ] **Step 4.3: Add the purge call at the top of `main()`**

In `email_fetcher.py`, find `def main():` and insert the retention block right after the config reads, before the `started = time.perf_counter()` line:

```python
def main():
    imap_server = config.get("imap_server")
    userid = config.get("userid")
    userpw = config.get("userpw")
    mailbox = config.get("mailbox")

    # Retention purge — runs before the fetch so we don't delete just-fetched rows
    retention_days = config.get("retention_days")
    if retention_days:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=retention_days)
        deleted = db.delete_emails_older_than(cutoff)
        if deleted:
            logging.info(f"Retention: purged {deleted} emails older than {retention_days} days.")

    # if emails.db does not exist since should be 30, otherwise 1
    since = 1
    if db.get_entry_count() == 0:
        since = 30

    started = time.perf_counter()
    ...
```

The rest of `main()` stays unchanged.

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
poetry run pytest tests/test_email_fetcher.py -v
```

Expected: all ~10 tests pass.

Run full suite:
```bash
poetry run pytest -v
```

Expected: ~92 tests total, all pass (79 pre-branch + 3 database retention + 5 connect + 2 fetch_emails + 3 main retention).

- [ ] **Step 4.5: Commit**

```bash
git add email_fetcher.py tests/test_email_fetcher.py
git commit -m "feat: run retention purge at start of fetcher main cycle"
```

---

## Task 5: Smoke + docs

**Files:**
- Modify: `README.md`

- [ ] **Step 5.1: Run the full suite**

```bash
poetry run pytest -v
```

Expected: full suite green, ~92 tests pass.

- [ ] **Step 5.2: Lint clean**

```bash
poetry run ruff check .
```

Expected: `All checks passed!`.

- [ ] **Step 5.3: Docker build sanity check**

```bash
docker build -f Dockerfile.serve -t email2rss-serve:sp3 .
docker build -f Dockerfile.fetch_and_generate -t email2rss-fetch:sp3 .
```

Expected: both build successfully. If docker isn't available, skip and note as DONE_WITH_CONCERNS — CI will catch.

- [ ] **Step 5.4: Update README**

In `README.md`, add a new config row to the "Configuration Options" table. Find the existing table (it has rows like `imap_server`, `userid`, `userpw`, etc.) and add:

```markdown
| `retention_days` | Delete emails older than N days at start of each fetch cycle; unset or `0` disables purging | unset |
```

Place it between the existing rows in a reasonable position (e.g., after `max_item_per_feed`).

Also add a short "Reliability" subsection somewhere under the existing feature description (near "Features" or at the end of the "How It Works" section):

```markdown
### Reliability

- **Connection retry** — `email_fetcher` retries the IMAP connection up to 4 times with exponential backoff (delays 0, 1, 2, 4 seconds) before giving up on a cycle.
- **Per-email resilience** — a malformed individual email (bad MIME, unparseable date) is logged and skipped rather than aborting the whole fetch cycle. IMAP-level errors (connection dropped) still abort the cycle so the next cycle can reconnect cleanly.
- **Retention** — set `retention_days=N` in `.env` to purge emails older than N days at the start of each fetch cycle.
```

- [ ] **Step 5.5: Commit docs**

```bash
git add README.md
git commit -m "docs: document retention config and fetcher reliability behavior"
```

---

## Acceptance criteria checklist

- [ ] `poetry run pytest -v` — ~92 tests pass
- [ ] `poetry run ruff check .` — clean
- [ ] Docker images build
- [ ] Manual: inject a malformed email (bad Date header) into the fetch path → other emails still persist
- [ ] Manual: set `retention_days=7` with pre-populated old rows → purge runs, row count drops
- [ ] Manual: `retention_days` unset → row count unchanged after fetch cycle
- [ ] Manual: IMAP server unreachable for ~3 seconds at start of cycle → connection retries, then succeeds on attempt 3 or 4
