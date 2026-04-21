"""Tests for img_proxy.py — HMAC signing, DNS pinning, SSRF defenses."""
import base64
import hmac as hmac_mod
import socket
from unittest import mock

import pytest
from werkzeug.exceptions import HTTPException

import img_proxy


TEST_SECRET = b"test-secret-do-not-use-in-prod"
TEST_BASE = "http://localhost:8000"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_sign_url_produces_deterministic_output():
    url = "http://example.com/x.png"
    a = img_proxy.sign_url(url, TEST_SECRET, TEST_BASE)
    b = img_proxy.sign_url(url, TEST_SECRET, TEST_BASE)
    assert a == b
    assert a.startswith("http://localhost:8000/img?u=")
    assert "&sig=" in a


def test_sign_url_encodes_url_as_urlsafe_base64():
    url = "http://example.com/x.png"
    signed = img_proxy.sign_url(url, TEST_SECRET, TEST_BASE)
    # Extract u= parameter
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(signed).query)
    assert "u" in q and "sig" in q
    decoded = base64.urlsafe_b64decode(q["u"][0] + "===").decode()
    assert decoded == url


def test_verify_signature_accepts_valid():
    url = "http://example.com/x.png"
    u_param = _b64(url)
    sig = img_proxy._compute_sig(u_param, TEST_SECRET)
    assert img_proxy.verify_signature(u_param, sig, TEST_SECRET) is True


def test_verify_signature_rejects_tampered():
    url = "http://example.com/x.png"
    u_param = _b64(url)
    sig = img_proxy._compute_sig(u_param, TEST_SECRET)
    bad = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert img_proxy.verify_signature(u_param, bad, TEST_SECRET) is False


def test_verify_signature_is_timing_safe():
    # Not a real timing test — just ensures it uses hmac.compare_digest,
    # which we verify by monkeypatching compare_digest and confirming it's called.
    url = "http://example.com/x.png"
    u_param = _b64(url)
    sig = img_proxy._compute_sig(u_param, TEST_SECRET)
    with mock.patch("img_proxy.hmac.compare_digest", wraps=hmac_mod.compare_digest) as spy:
        assert img_proxy.verify_signature(u_param, sig, TEST_SECRET) is True
        assert spy.called


def test_fetch_image_rejects_non_http_scheme():
    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("file:///etc/passwd", TEST_SECRET)
    assert ei.value.code == 400


def test_fetch_image_rejects_private_ipv4(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://evil.example.com/x.png", TEST_SECRET)
    assert ei.value.code == 403


def test_fetch_image_rejects_loopback(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://evil.example.com/x.png", TEST_SECRET)
    assert ei.value.code == 403


def test_fetch_image_rejects_link_local(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://169.254.169.254/latest/meta-data", TEST_SECRET)
    assert ei.value.code == 403


def test_fetch_image_rejects_private_ipv6(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fc00::1", port, 0, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://evil.example.com/x.png", TEST_SECRET)
    assert ei.value.code == 403


def test_fetch_image_rejects_when_any_resolved_ip_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://mixed.example.com/x.png", TEST_SECRET)
    assert ei.value.code == 403


def _fake_response(status, headers, body_chunks):
    """Return a minimal duck-typed response for requests.Session.send patching."""
    class _FakeRaw:
        """Stub for r.raw — just enough for extract_cookies_to_jar to no-op."""
        _original_response = None

    class R:
        status_code = status
        # Attributes needed when the response passes through Session.send
        # (used when patching at HTTPAdapter.send level rather than Session.send)
        is_redirect = False
        history = []
        raw = _FakeRaw()

        def __init__(self):
            self.headers = headers
            self._chunks = list(body_chunks)

        def iter_content(self, chunk_size=None):
            return iter(self._chunks)

        def close(self):
            pass
    return R()


def test_fetch_image_happy_path_png(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    png_body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    def fake_send(adapter, req, **kw):
        # PinnedIPAdapter MUST rewrite the URL to the pinned IP
        assert req.url.startswith("http://93.184.216.34:80/"), f"expected pinned IP URL, got {req.url}"
        # ...and MUST preserve the original hostname in the Host header
        assert req.headers.get("Host") == "example.com", f"expected Host=example.com, got {req.headers.get('Host')}"
        return _fake_response(200, {"Content-Type": "image/png"}, [png_body])

    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", fake_send)

    body, ctype = img_proxy.fetch_image("http://example.com/x.png", TEST_SECRET)
    assert body == png_body
    assert ctype == "image/png"


def test_fetch_image_rejects_html_content_type(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def fake_send(session, req, **kw):
        return _fake_response(200, {"Content-Type": "text/html"}, [b"<html>"])
    monkeypatch.setattr("requests.Session.send", fake_send)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://example.com/x", TEST_SECRET)
    assert ei.value.code == 415


def test_fetch_image_rejects_svg_content_type(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def fake_send(session, req, **kw):
        return _fake_response(200, {"Content-Type": "image/svg+xml"}, [b"<svg/>"])
    monkeypatch.setattr("requests.Session.send", fake_send)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://example.com/x.svg", TEST_SECRET)
    assert ei.value.code == 415


def test_fetch_image_rejects_oversized(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    big_chunks = [b"A" * 1_048_576] * 6  # 6 MB streamed

    def fake_send(session, req, **kw):
        return _fake_response(200, {"Content-Type": "image/png"}, big_chunks)
    monkeypatch.setattr("requests.Session.send", fake_send)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://example.com/x.png", TEST_SECRET)
    assert ei.value.code == 413


def test_fetch_image_rejects_non_200(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def fake_send(session, req, **kw):
        return _fake_response(302, {"Location": "http://other/"}, [])
    monkeypatch.setattr("requests.Session.send", fake_send)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://example.com/x", TEST_SECRET)
    assert ei.value.code == 502


def test_fetch_image_timeout_becomes_502(monkeypatch):
    import requests as requests_mod

    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def fake_send(session, req, **kw):
        raise requests_mod.Timeout("simulated timeout")
    monkeypatch.setattr("requests.Session.send", fake_send)

    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://example.com/x", TEST_SECRET)
    assert ei.value.code == 502


def test_fetch_image_rejects_cgnat(monkeypatch):
    """100.64.0.0/10 is CGNAT; not is_private on Python 3.11+, but not is_global either."""
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://cgnat.example.com/x.png", TEST_SECRET)
    assert ei.value.code == 403


def test_fetch_image_rejects_documentation_range(monkeypatch):
    """192.0.2.0/24 is TEST-NET-1 per RFC 5737; not is_global."""
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(HTTPException) as ei:
        img_proxy.fetch_image("http://docs.example.com/x.png", TEST_SECRET)
    assert ei.value.code == 403


def test_pinned_ip_adapter_sets_tls_sni_for_https(monkeypatch):
    """After URL rewrite to pinned IP, TLS still needs the real hostname.

    Contract:
    - `server_hostname` goes into `conn_kw` (urllib3's channel for SNI)
    - `assert_hostname` goes on the pool as an attribute (NOT in conn_kw);
      urllib3 passes `assert_hostname` as an explicit kwarg at connection
      creation, so putting it in `conn_kw` too would raise TypeError
      'got multiple values for keyword argument' (prod regression 2026-04).
    """
    import requests
    import requests.adapters

    adapter = img_proxy.PinnedIPAdapter(pinned_host="example.com", pinned_ip="93.184.216.34")

    class FakePool:
        def __init__(self):
            self.conn_kw = {}
            self.assert_hostname = None  # urllib3 default

    fake_conn = FakePool()

    def fake_super_get(self, request, verify, proxies=None, cert=None):
        return fake_conn

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter,
        "get_connection_with_tls_context",
        fake_super_get,
    )

    req = requests.Request("GET", "https://93.184.216.34:443/x.png").prepare()
    conn = adapter.get_connection_with_tls_context(req, verify=True)

    # server_hostname channel: conn_kw
    assert conn.conn_kw.get("server_hostname") == "example.com"
    # assert_hostname channel: pool attribute, NOT conn_kw
    assert conn.assert_hostname == "example.com"
    assert "assert_hostname" not in conn.conn_kw, (
        "putting assert_hostname in conn_kw collides with urllib3's explicit "
        "kwarg and raises TypeError at connection creation"
    )
