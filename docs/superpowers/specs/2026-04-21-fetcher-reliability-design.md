# Fetcher Reliability

**Status:** Approved design, ready for implementation plan
**Date:** 2026-04-21
**Scope:** Sub-project 3 of 4 in the broader reliability/feature improvement initiative

## Purpose

Make the IMAP fetcher tolerant of transient and per-email failures, and keep the database from growing forever.

Today a single malformed email or a brief network hiccup aborts the entire fetch cycle — all emails in that cycle are lost until the next poll. The database has no retention policy, so rows accumulate indefinitely. This sub-project fixes both.

## Non-goals

- IMAP IDLE / near-real-time notifications. Polling latency (≤ `refresh_seconds`, default 300s) is adequate for newsletters. IDLE is a standalone future project if needed.
- Per-sender failure tracking or backoff. Per-email try/except captures the bulk of the value; persistent sender state adds complexity without a real win for a single-user tool.
- Purging orphaned feed XML files. Disk overhead from stale XML is tiny; DB row purging is the main concern.
- OAuth2 for IMAP. Separate scope.

## Architecture

### Connection retry

`connect_to_gmail()` wraps its inner logic in an exponential-backoff retry loop — 4 attempts total, delays `[0, 1, 2, 4]` seconds. Retries on `imaplib.IMAP4.error` and `OSError`. Non-network exceptions (`KeyboardInterrupt`, `SystemExit`) propagate unchanged.

```python
delays = [0, 1, 2, 4]
last_err = None
for attempt, delay in enumerate(delays):
    if delay:
        time.sleep(delay)
    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(username, password)
        mail.select(mailbox)
        return mail
    except (imaplib.IMAP4.error, OSError) as e:
        last_err = e
        logging.warning(f"IMAP connect attempt {attempt + 1} failed: {e}")
raise last_err
```

### Per-email resilience

`fetch_emails()` distinguishes two error classes inside the per-message loop:

- **IMAP-level errors** (`imaplib.IMAP4.error`, `OSError`) — connection is in an unknown state. Log, re-raise, abort the cycle. The outer `main()` catches and the next cycle reconnects from scratch.
- **Per-email errors** (anything else — malformed MIME, bad date, DB constraint violation) — log with `logging.exception` (includes traceback), skip this message, continue the loop.

```python
for index, num in enumerate(messages):
    try:
        _, data = mail.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])
        sender = extract_email_address(msg["from"], default="unknown@email.com")
        receiver = extract_email_address(msg["to"], default="you@email.com")
        article_date = email.utils.parsedate_to_datetime(msg["date"])
        db.save_email(
            sender=sender, receiver=receiver,
            subject=msg["subject"], email_id=int(num),
            content=data[0][1], timestamp=article_date,
        )
        _emails_received.add(1, {"sender": sender})
    except (imaplib.IMAP4.error, OSError) as e:
        logging.error(f"IMAP error while fetching message {num}: {e}")
        raise
    except Exception:
        logging.exception(f"Skipping malformed email num={num}")
        continue
```

### Retention

New config key `retention_days` (int, default `None`). `0` or unset is treated as disabled.

At the top of `email_fetcher.main()` — before connecting to IMAP — if `retention_days` is truthy, compute `cutoff = utcnow() - timedelta(days=retention_days)` and call `db.delete_emails_older_than(cutoff)`. Log the row count when nonzero.

New `database.delete_emails_older_than(cutoff) -> int`:

```python
def delete_emails_older_than(cutoff: datetime.datetime) -> int:
    with Session() as session:
        deleted = (
            session.query(Email)
            .filter(Email.timestamp < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
    return deleted
```

**No `VACUUM`.** The app runs with `PRAGMA journal_mode=WAL` and two concurrent processes (`fetch_and_generate` and `serve`). `VACUUM` requires an exclusive lock that conflicts with the reader's open read transaction, producing spurious `database is locked` errors. SQLite reuses freed pages for subsequent inserts, so the row count stays bounded; the file size doesn't shrink but doesn't grow unbounded either. If users ever need to reclaim on-disk space, they can stop the app and run `sqlite3 emails.db "VACUUM"` by hand.

### Config addition

In `common.py`:

```python
"retention_days": int(os.getenv("retention_days", "0")) or None,
```

`0` becomes falsy, treated as unset. Any positive int enables purging.

## File changes

| File | Action | Responsibility |
|------|--------|----------------|
| `email_fetcher.py` | modify | `connect_to_gmail` retry loop, per-email try/except in `fetch_emails`, purge call at top of `main()` |
| `database.py` | modify | Add `delete_emails_older_than(cutoff)` |
| `common.py` | modify | Add `retention_days` config entry |
| `tests/test_email_fetcher.py` | **create** | Mocked-IMAP tests for retry, per-email resilience, retention wiring |
| `tests/test_database.py` | modify | Tests for `delete_emails_older_than` |

## Testing

### `tests/test_email_fetcher.py` (new)

- `test_connect_to_gmail_succeeds_on_first_attempt` — happy path
- `test_connect_to_gmail_retries_on_oserror` — first 2 attempts raise `OSError`, 3rd succeeds → 3 constructor calls
- `test_connect_to_gmail_retries_on_imap_error` — login raises `imaplib.IMAP4.error` on first attempt, succeeds on second
- `test_connect_to_gmail_raises_after_all_retries_fail` — all 4 attempts fail → final exception re-raised
- `test_connect_to_gmail_applies_exponential_backoff` — monkeypatch `time.sleep`, assert it was called with 1, 2, 4 between attempts (not before the first)
- `test_fetch_emails_continues_past_malformed_email` — 3 messages, middle one's MIME parsing explodes → other 2 saved; `db.get_entry_count() == 2`
- `test_fetch_emails_aborts_on_imap_error` — fetch on message 2 raises `imaplib.IMAP4.error` → exception propagates; message 1 was saved, message 3 was not
- `test_main_calls_purge_when_retention_days_set` — `retention_days=7`; mock `db.delete_emails_older_than` → called once with a cutoff approximately 7 days ago (use `abs(cutoff - expected) < timedelta(minutes=1)`)
- `test_main_skips_purge_when_retention_days_none` — `retention_days=None` → `delete_emails_older_than` NOT called

Fixture pattern for patching IMAP:

```python
@pytest.fixture
def fake_imap(monkeypatch):
    import imaplib
    from unittest.mock import MagicMock
    mock_class = MagicMock()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", mock_class)
    return mock_class
```

### `tests/test_database.py` (additions)

- `test_delete_emails_older_than_deletes_matching_rows` — insert 5 rows at known timestamps; purge with midpoint cutoff → expected rows gone, expected rows remain
- `test_delete_emails_older_than_returns_count` — insert 3 old + 2 new → return value is 3
- `test_delete_emails_older_than_zero_rows_is_noop` — no matching rows → returns 0, row count unchanged

## Acceptance criteria

1. Full suite passes — ~79 + ~12 = ~91 tests.
2. A synthetic malformed email inserted into the fetch stream does not kill the cycle; surrounding emails still land in the DB.
3. With `retention_days=7` in `.env`, running the fetcher once against a DB pre-populated with emails older than 7 days purges those rows; subsequent query returns only recent rows.
4. With `retention_days` unset (or `0`), the DB row count is unchanged after a fetch cycle.
5. With the IMAP server unreachable on the first 2 attempts, connection retry succeeds on attempt 3 and the fetch proceeds; no stacktrace in logs at ERROR level.
6. Ruff clean, Docker builds green, CI green.

## Out of scope (explicitly deferred)

- IMAP IDLE
- Per-sender backoff / failure counters
- Persistent backoff state across restarts
- Orphan feed XML file cleanup
- OAuth2 IMAP auth
- Rate-limit detection / 429 handling (IMAP doesn't have a 429 concept)
- Alerting / notification on repeated connect failures
