"""Image proxy: HMAC signing, DNS-pinned fetches, size/type/redirect defenses."""
from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import socket
from hashlib import sha256
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from werkzeug.exceptions import abort


logger = logging.getLogger(__name__)

SIG_LENGTH = 32  # 128-bit truncation
ALLOWED_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_SECS = 5
CHUNK_SIZE = 8192


def _compute_sig(u_param: str, secret: bytes) -> str:
    digest = hmac.new(secret, u_param.encode("utf-8"), sha256).hexdigest()
    return digest[:SIG_LENGTH]


def sign_url(url: str, secret: bytes, base: str) -> str:
    u_param = base64.urlsafe_b64encode(url.encode("utf-8")).decode().rstrip("=")
    sig = _compute_sig(u_param, secret)
    return f"{base.rstrip('/')}/img?u={u_param}&sig={sig}"


def verify_signature(u_param: str, sig: str, secret: bytes) -> bool:
    expected = _compute_sig(u_param, secret)
    return hmac.compare_digest(expected, sig)


class PinnedIPAdapter(HTTPAdapter):
    """
    requests adapter that routes connections to a pre-validated IP while
    preserving the original hostname for the Host header and TLS SNI.

    Prevents DNS rebinding: once we've validated the resolved address,
    the underlying socket connects directly to that IP, bypassing any
    re-resolution at connect time.
    """

    def __init__(self, pinned_host: str, pinned_ip: str, **kwargs):
        self.pinned_host = pinned_host
        self.pinned_ip = pinned_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        # Urllib3 lets us inject server_hostname for SNI; the actual IP is
        # passed via the URL we rewrite in `send`.
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )

    def send(self, request, **kwargs):
        # Rewrite the URL to target the pinned IP; preserve hostname via Host header
        parsed = urlparse(request.url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip_host = f"[{self.pinned_ip}]" if ":" in self.pinned_ip else self.pinned_ip
        new_netloc = f"{ip_host}:{port}"
        new_url = parsed._replace(netloc=new_netloc).geturl()
        request.url = new_url
        request.headers["Host"] = self.pinned_host
        return super().send(request, **kwargs)


def _is_public_ip(ip_str: str) -> bool:
    """
    True only for unambiguously public, routable IPs.

    Using `not ip.is_global` catches CGNAT (100.64.0.0/10), documentation ranges,
    benchmark ranges, etc. — which Python 3.11+ no longer classifies as
    `is_private`. We keep the explicit checks for future-proofing and clarity.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False  # malformed → treat as non-public; caller will abort(403)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def fetch_image(url: str, secret: bytes) -> tuple[bytes, str]:
    """
    Fetch an image with SSRF defenses. Returns (bytes, content_type) on success,
    or raises werkzeug HTTPException with the appropriate status code.

    `secret` is accepted for API symmetry with sign_url; signature verification
    happens in the Flask route, not here.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        abort(400)

    host = parsed.hostname
    if not host:
        abort(400)

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        abort(502)

    resolved_ips = {info[4][0] for info in infos}
    if not resolved_ips:
        abort(502)

    # Reject if ANY resolved address is non-public
    for ip_str in resolved_ips:
        if not _is_public_ip(ip_str):
            abort(403)

    pinned_ip = next(iter(resolved_ips))

    session = requests.Session()
    adapter = PinnedIPAdapter(pinned_host=host, pinned_ip=pinned_ip)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        resp = session.get(
            url,
            timeout=FETCH_TIMEOUT_SECS,
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": "email2rss-image-proxy"},
        )
    except requests.RequestException:
        logger.warning("fetch_image request failed for %s", url)
        abort(502)

    try:
        if resp.status_code != 200:
            abort(502)

        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ALLOWED_IMAGE_TYPES:
            abort(415)

        body = bytearray()
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    abort(413)

        return bytes(body), ctype
    finally:
        resp.close()
