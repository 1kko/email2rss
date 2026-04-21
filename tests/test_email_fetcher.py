"""Tests for email_fetcher — connection retry, per-email resilience, retention wiring."""
import imaplib
from unittest.mock import MagicMock

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
