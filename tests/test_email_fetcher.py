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
