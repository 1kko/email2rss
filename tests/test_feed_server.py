"""Route tests for feed_server.py."""
import base64 as _b64mod
import hashlib
import socket as _socket_mod

import pytest

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


def test_feed_xml_served_from_feed_dir(client, tmp_path):
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


def test_article_route_404s_when_guid_unknown(client, db_session):
    insert_email(db_session, email_id=1)
    resp = client.get("/article/sender_example_com/nonexistent_guid")
    assert resp.status_code == 404


def test_article_route_renders_body_in_sandboxed_iframe(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")

    insert_email(db_session, email_id=1)

    # Build the GUID using the known fixture values
    import hashlib
    subject = "Hello from the test suite"
    date_str = "Mon, 13 Apr 2026 10:00:00 +0000"
    sender = "sender@example.com"
    guid = hashlib.md5(
        (subject + date_str + sender).encode(), usedforsecurity=False
    ).hexdigest()

    resp = client.get(f"/article/sender_example_com/{guid}")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert '<iframe' in html
    assert 'sandbox="allow-popups allow-popups-to-escape-sandbox"' in html
    assert 'srcdoc=' in html
    assert "Hello from the test suite" in html  # subject in header


def test_article_route_404s_for_unknown_feed(client, db_session):
    resp = client.get("/article/who_knows_com/abcdef")
    assert resp.status_code == 404


def _build_signed_img_url(original_url, secret):
    import img_proxy
    u = _b64mod.urlsafe_b64encode(original_url.encode()).decode().rstrip("=")
    sig = img_proxy._compute_sig(u, secret)
    return f"/img?u={u}&sig={sig}"


def test_img_route_rejects_bad_signature(client, monkeypatch):
    resp = client.get("/img?u=aHR0cDovL2V4YW1wbGUuY29tL3gucG5n&sig=deadbeef")
    assert resp.status_code == 403


def test_img_route_rejects_missing_signature(client):
    resp = client.get("/img?u=aHR0cDovL2V4YW1wbGUuY29tL3gucG5n")
    assert resp.status_code == 403


def test_img_route_rejects_missing_u(client):
    resp = client.get("/img?sig=deadbeef")
    assert resp.status_code == 400


def test_img_route_happy_path(client, monkeypatch):
    import common

    secret = common.get_img_proxy_secret()
    url = _build_signed_img_url("http://example.com/x.png", secret)

    def fake_getaddrinfo(host, port, **kw):
        return [(_socket_mod.AF_INET, _socket_mod.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(_socket_mod, "getaddrinfo", fake_getaddrinfo)

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        def iter_content(self, chunk_size=None):
            return iter([png])

        def close(self):
            pass

    def fake_send(self, req, **kw):
        return FakeResp()
    monkeypatch.setattr("requests.Session.send", fake_send)

    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.headers.get("Cache-Control", "").startswith("public")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.data == png


def test_article_route_srcdoc_contains_inner_csp(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")

    insert_email(db_session, email_id=1)
    import hashlib
    guid = hashlib.md5(
        ("Hello from the test suite"
         + "Mon, 13 Apr 2026 10:00:00 +0000"
         + "sender@example.com").encode(),
        usedforsecurity=False,
    ).hexdigest()

    resp = client.get(f"/article/sender_example_com/{guid}")
    html = resp.data.decode("utf-8")
    # The srcdoc attribute has HTML-escaped content; decode entities:
    import html as html_mod
    srcdoc_start = html.index('srcdoc="') + len('srcdoc="')
    srcdoc_end = html.index('"', srcdoc_start)
    srcdoc_escaped = html[srcdoc_start:srcdoc_end]
    srcdoc_decoded = html_mod.unescape(srcdoc_escaped)
    assert "default-src 'none'" in srcdoc_decoded
    assert "img-src http://testserver data:" in srcdoc_decoded


def test_article_route_has_tightened_outer_csp(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    monkeypatch.setitem(feed_server.config, "enable_internal_reader", True)
    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")

    insert_email(db_session, email_id=1)
    import hashlib
    guid = hashlib.md5(
        ("Hello from the test suite"
         + "Mon, 13 Apr 2026 10:00:00 +0000"
         + "sender@example.com").encode(),
        usedforsecurity=False,
    ).hexdigest()

    resp = client.get(f"/article/sender_example_com/{guid}")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "img-src 'self' data:" in csp
    assert "img-src *" not in csp
    assert "frame-src 'self'" in csp


def test_main_aborts_when_reader_enabled_without_baseurl(monkeypatch):
    """Task 6 gap: validate_reader_config must run at startup."""
    import feed_server
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", True)
    monkeypatch.setitem(common.config, "server_baseurl", None)

    # Intercept before it actually calls .run() or mkdir. Stub everything downstream.
    monkeypatch.setattr(feed_server, "FEED_DIR", feed_server.FEED_DIR)  # no-op
    # Replace app.run so the test doesn't actually start a server
    monkeypatch.setattr(feed_server.app, "run", lambda **kw: None)

    with pytest.raises(RuntimeError, match="server_baseurl"):
        feed_server.main()


def _guid_for_default_fixture():
    """Compute the GUID for insert_email's default values."""
    import hashlib
    return hashlib.md5(
        ("Hello from the test suite"
         + "Mon, 13 Apr 2026 10:00:00 +0000"
         + "sender@example.com").encode(),
        usedforsecurity=False,
    ).hexdigest()


def test_mark_read_route_sets_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    resp = client.post(f"/article/sender_example_com/{guid}/read")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_read": True}

    refreshed = db_session.query(feed_server.db.Email).filter_by(id=row.id).one()
    assert refreshed.is_read is True


def test_unmark_read_route_clears_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    feed_server.db.mark_read(row.id, True)
    guid = _guid_for_default_fixture()

    resp = client.delete(f"/article/sender_example_com/{guid}/read")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_read": False}


def test_star_route_sets_flag(client, db_session):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    resp = client.post(f"/article/sender_example_com/{guid}/star")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_starred": True}


def test_unstar_route_clears_flag(client, db_session):
    from tests.conftest import insert_email
    row = insert_email(db_session, email_id=1)
    feed_server.db.mark_starred(row.id, True)
    guid = _guid_for_default_fixture()

    resp = client.delete(f"/article/sender_example_com/{guid}/star")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_starred": False}


def test_mark_read_route_404s_for_unknown_guid(client, db_session):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    resp = client.post("/article/sender_example_com/nonexistentguid123/read")
    assert resp.status_code == 404


def test_star_route_rejects_cross_origin(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")

    resp = client.post(
        f"/article/sender_example_com/{guid}/star",
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_star_route_allows_missing_origin(client, db_session):
    """No Origin header (e.g. curl) is allowed through."""
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    guid = _guid_for_default_fixture()

    resp = client.post(f"/article/sender_example_com/{guid}/star")
    assert resp.status_code == 200


def test_article_list_filter_unread(client, db_session):
    from tests.conftest import insert_email
    a = insert_email(db_session, email_id=1, sender="s@example.com")
    _b = insert_email(db_session, email_id=2, sender="s@example.com")
    feed_server.db.mark_read(a.id, True)  # a is read

    resp = client.get("/article?filter=unread")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Unread list should contain b but not a
    assert body.count('class="unread"') == 1  # one row with unread class


def test_article_list_filter_starred(client, db_session):
    from tests.conftest import insert_email
    _a = insert_email(db_session, email_id=1, sender="s@example.com")
    b = insert_email(db_session, email_id=2, sender="s@example.com")
    feed_server.db.mark_starred(b.id, True)

    resp = client.get("/article?filter=starred")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Only one row should appear (the starred one)
    assert body.count("<li ") == 1   # <li class="..." ...>


def test_article_list_default_filter_is_all(client, db_session):
    from tests.conftest import insert_email
    insert_email(db_session, email_id=1)
    insert_email(db_session, email_id=2)

    resp = client.get("/article")  # no filter param
    body = resp.data.decode("utf-8")
    # Both rows present
    assert body.count("<li ") == 2


def test_search_route_returns_results(client, db_session):
    feed_server.db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=100,
        subject="ThirdQuarterFinancials",
        content=b"From: s@example.com\nSubject: ThirdQuarterFinancials\n\nreport body",
        timestamp=__import__("datetime").datetime(2026, 4, 13),
    )

    resp = client.get("/search?q=ThirdQuarterFinancials")
    assert resp.status_code == 200
    assert b"ThirdQuarterFinancials" in resp.data


def test_search_route_empty_query_renders_prompt(client, db_session):
    resp = client.get("/search")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Enter a query" in body or "search box above" in body


def test_search_route_invalid_query_renders_error(client, db_session):
    resp = client.get("/search?q=AND%20AND%20AND")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Search error" in body or "error" in body.lower()
