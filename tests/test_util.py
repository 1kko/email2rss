"""Tests for util.py — pure helper functions."""
from util import (
    cleanse_content,
    extract_domain_address,
    extract_email_address,
    sanitize_html,
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


def test_sanitize_html_removes_script_and_event_handlers():
    dirty = '<p onclick="alert(1)">hi</p><script>evil()</script><a href="javascript:bad()">x</a>'
    clean = sanitize_html(dirty)
    assert "<script>" not in clean
    assert "evil()" not in clean
    assert "onclick" not in clean
    assert "javascript:" not in clean
    assert "hi" in clean  # content preserved
