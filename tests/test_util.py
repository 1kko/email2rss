"""Tests for util.py — pure helper functions."""
import datetime

from util import (
    cleanse_content,
    extract_domain_address,
    extract_email_address,
    monogram_hue,
    relative_date,
)


def test_extract_email_address_from_formatted_string():
    assert extract_email_address("Sender Name <hello@Example.COM>") == "hello@example.com"


def test_extract_email_address_returns_default_when_missing():
    assert extract_email_address("no address here", default="fallback@x.com") == "fallback@x.com"


def test_extract_domain_address():
    assert extract_domain_address("user@sub.example.com") == "sub.example.com"
    assert extract_domain_address("no-at-sign", default="unknown") == "unknown"


def test_cleanse_content_strips_control_chars_but_keeps_whitespace():
    raw = "keep\ttab\nnewline\rcr\x00null\x08backspace"
    assert cleanse_content(raw) == "keep\ttab\nnewline\rcrnullbackspace"


def _now():
    return datetime.datetime(2026, 4, 21, 15, 0, 0)


def test_relative_date_just_now():
    dt = _now() - datetime.timedelta(seconds=30)
    assert relative_date(dt, now=_now()) == "방금 전"


def test_relative_date_minutes():
    dt = _now() - datetime.timedelta(minutes=5)
    assert relative_date(dt, now=_now()) == "5분 전"


def test_relative_date_hours():
    dt = _now() - datetime.timedelta(hours=3)
    assert relative_date(dt, now=_now()) == "3시간 전"


def test_relative_date_yesterday():
    dt = _now() - datetime.timedelta(hours=20)  # previous calendar day
    assert relative_date(dt, now=_now()) == "어제"


def test_relative_date_days():
    dt = _now() - datetime.timedelta(days=3)
    assert relative_date(dt, now=_now()) == "3일 전"


def test_relative_date_weeks():
    dt = _now() - datetime.timedelta(days=10)
    assert relative_date(dt, now=_now()) == "1주 전"


def test_relative_date_months():
    dt = _now() - datetime.timedelta(days=60)
    assert relative_date(dt, now=_now()) == "2개월 전"


def test_relative_date_years():
    dt = _now() - datetime.timedelta(days=400)
    assert relative_date(dt, now=_now()) == "1년 전"


def test_relative_date_accepts_aware_datetime():
    """tz-aware input shouldn't crash — compare in UTC."""
    now_aware = datetime.datetime(2026, 4, 21, 15, 0, 0, tzinfo=datetime.timezone.utc)
    dt = now_aware - datetime.timedelta(hours=2)
    assert relative_date(dt, now=now_aware) == "2시간 전"


def test_monogram_hue_is_deterministic():
    assert monogram_hue("alice@example.com") == monogram_hue("alice@example.com")
    assert 0 <= monogram_hue("alice@example.com") < 360


def test_monogram_hue_differs_for_different_senders():
    # Almost any hash fn will distinguish these two
    assert monogram_hue("alice@example.com") != monogram_hue("bob@example.com")
