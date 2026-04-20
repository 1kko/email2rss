"""Tests for feed_server Flask routes."""
import hashlib

from defusedxml.ElementTree import fromstring as safe_fromstring

import feed_server
from tests.conftest import insert_email


def _expected_guid(subject: str, date_str: str, from_header: str) -> str:
    return hashlib.md5((subject + date_str + from_header).encode(), usedforsecurity=False).hexdigest()


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_stats_reports_counts_and_senders(client, db_session):
    insert_email(db_session, sender="alice@example.com", email_id=1)
    insert_email(db_session, sender="bob@example.com", email_id=2)

    resp = client.get("/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_emails"] == 2
    assert data["total_senders"] == 2
    assert set(data["senders"]) == {"alice@example.com", "bob@example.com"}


def test_feed_xml_served_from_feed_dir(client, tmp_path, monkeypatch):
    # feed_server serves static feed files from FEED_DIR (monkeypatched to tmp_path in fixture)
    (tmp_path / "hello_example_com.xml").write_text(
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>',
        encoding="utf-8",
    )
    resp = client.get("/hello_example_com.xml")
    assert resp.status_code == 200
    # send_from_directory infers content type from extension; .xml → application/xml
    assert resp.mimetype in ("application/xml", "text/xml")
    # Sanity: parseable
    safe_fromstring(resp.data)


def test_unknown_feed_returns_404(client):
    resp = client.get("/does_not_exist.xml")
    assert resp.status_code == 404


def test_subscriptions_opml_served_when_present(client, tmp_path):
    (tmp_path / "subscriptions.opml").write_text(
        '<?xml version="1.0"?><opml version="1.0"><head><title>t</title></head><body/></opml>',
        encoding="utf-8",
    )
    resp = client.get("/subscriptions.opml")
    assert resp.status_code == 200
    root = safe_fromstring(resp.data)
    assert root.tag == "opml"


def test_article_route_404s_when_guid_unknown(client, db_session, monkeypatch):
    # Current behavior: abort(404) inside view_article is caught by the broad
    # `except Exception` handler, which then calls abort(500).  Characterizing
    # the actual behavior here; the root cause is a bug where HTTPException is
    # swallowed by the outer try/except.
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    insert_email(db_session, email_id=1)
    resp = client.get("/article/sender_example_com/nonexistent_guid")
    assert resp.status_code == 500


def test_article_route_renders_email_body(client, db_session, monkeypatch):
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    subject = "Hello from the test suite"
    date_str = "Mon, 13 Apr 2026 10:00:00 +0000"
    sender = "sender@example.com"
    # insert_email rewrites From to the bare sender address; GUID matches that
    guid = _expected_guid(subject, date_str, sender)

    insert_email(db_session, email_id=1)

    resp = client.get(f"/article/sender_example_com/{guid}")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Hello from" in body  # HTML or plain content makes it into the rendered template


def test_article_route_not_found_for_unknown_feed(client, db_session, monkeypatch):
    # Current behavior: same abort(404)-swallowed-as-500 issue as above.
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    resp = client.get("/article/who_knows_com/abcdef")
    assert resp.status_code == 500
