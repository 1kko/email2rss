"""Route tests for feed_server.py.

Two tests (test_article_route_swallows_404_as_500_*) are characterization
tests that pin a known bug: abort(404) inside view_article is swallowed by
a broad `except Exception` handler and re-raised as 500. These tests should
be updated to assert 404 when the bug is fixed.
"""
import base64 as _b64mod
import hashlib
import socket as _socket_mod

from defusedxml.ElementTree import fromstring as safe_fromstring

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


def test_article_route_swallows_404_as_500_when_guid_unknown(client, db_session):
    insert_email(db_session, email_id=1)
    resp = client.get("/article/sender_example_com/nonexistent_guid")
    # BUG feed_server.py view_article — `try/except Exception` catches the NotFound
    # raised by abort(404) and re-aborts as 500. Characterized here; fix should
    # narrow the catch (e.g., `except HTTPException: raise`) and flip assertion.
    assert resp.status_code == 500


def test_article_route_renders_email_body(client, db_session):
    subject = "Hello from the test suite"
    date_str = "Mon, 13 Apr 2026 10:00:00 +0000"
    sender = "sender@example.com"
    # insert_email rewrites From to the bare sender address; GUID matches that
    guid = _expected_guid(subject, date_str, sender)

    insert_email(db_session, email_id=1)

    resp = client.get(f"/article/sender_example_com/{guid}")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.data.decode("utf-8")
    # Subject rendered into the template; sender email also rendered as metadata
    assert "Hello from the test suite" in body
    assert sender in body


def test_article_route_swallows_404_as_500_for_unknown_feed(client, db_session):
    resp = client.get("/article/who_knows_com/abcdef")
    # BUG feed_server.py view_article — `try/except Exception` catches the NotFound
    # raised by abort(404) and re-aborts as 500. Characterized here; fix should
    # narrow the catch (e.g., `except HTTPException: raise`) and flip assertion.
    assert resp.status_code == 500


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
