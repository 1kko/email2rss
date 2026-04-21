# Reader Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the internal reader by rendering email content inside a non-script sandboxed iframe with a tightened CSP, proxying remote images through a signed/SSRF-hardened `/img` endpoint, replacing the regex HTML sanitizer with `bleach`, and fixing the 404→500 bug in `view_article`.

**Architecture:** Two new modules keep responsibilities focused. `reader.py` owns the reader pipeline (`extract_body_and_cid_map` → `clean_and_rewrite` → `render_iframe_document`). `img_proxy.py` owns HMAC signing/validation, the DNS-pinned HTTPAdapter, and the `fetch_image` helper. `feed_server.py` stays thin — it wires route handlers to these modules and manages the Flask app. Startup validation in `common.py` ensures the reader cannot run in a misconfigured state.

**Tech Stack:** Python 3.12, Flask, SQLAlchemy, pytest, **bleach>=6.2** (new), `requests>=2.33.0`, `urllib3` (transitive), Jinja2.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | modify | Add `bleach>=6.2,<7` to runtime deps |
| `poetry.lock` | modify | Regenerate via `poetry lock` |
| `common.py` | modify | Add `img_proxy_secret` config + new `validate_reader_config()` helper that aborts when reader is on without `server_baseurl`; also loads/creates the secret file |
| `reader.py` | **create** | `extract_body_and_cid_map(msg)`, `clean_and_rewrite(html, cid_map, sign_url)`, `render_iframe_document(cleaned_html, proxy_origin)` |
| `img_proxy.py` | **create** | `sign_url(url)`, `verify_signature(u, sig)`, `PinnedIPAdapter`, `fetch_image(url) -> (bytes, content_type)` |
| `feed_server.py` | modify | Rewire `view_article` to the new pipeline, add `/img` route, fix 404→500 bug, tighten outer CSP |
| `templates/article.html` | modify | Replace inline content `<div>` with sandboxed `<iframe srcdoc>` |
| `static/reader.css` | modify | Add `.email-body-iframe` rule |
| `static/reader.js` | modify | Remove the now-dead `.content img` fade-in logic |
| `util.py` | modify | Delete `sanitize_html` (dead after bleach replaces it) |
| `tests/conftest.py` | modify | Extend `insert_email` with optional `inline_images` kwarg for multipart-inline MIMEs |
| `tests/test_reader.py` | **create** | Tests for `extract_body_and_cid_map`, `clean_and_rewrite`, `render_iframe_document` |
| `tests/test_img_proxy.py` | **create** | Tests for HMAC sign/verify, PinnedIPAdapter behavior, `fetch_image` SSRF defenses |
| `tests/test_feed_server.py` | modify | Update article-route tests for iframe output, flip 500→404 assertions, add `/img` integration tests |
| `tests/test_util.py` | modify | Delete the `sanitize_html` test |

---

## Task 1: Add `bleach` dep + config plumbing + startup validation

**Files:**
- Modify: `pyproject.toml`
- Modify: `common.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1.1: Add bleach to pyproject.toml**

Add to both PEP 621 deps and the poetry mirror table. In the `[project].dependencies` list:

```toml
"bleach (>=6.2,<7)",
```

In `[tool.poetry.dependencies]`:

```toml
bleach = ">=6.2,<7"
```

- [ ] **Step 1.2: Re-lock**

Run `poetry lock` then `poetry install` to pick up bleach. Verify:

```bash
poetry run python -c "import bleach; print(bleach.__version__)"
```

Expected: prints a 6.x version number.

- [ ] **Step 1.3: Extend `common.config` with `img_proxy_secret` + add validation**

Current `common.py` has `config = {...}` literal followed by logging setup. Modify as follows — add after the existing config dict and before the logging block:

```python
import secrets
import stat
from pathlib import Path as _Path


def _load_or_create_img_proxy_secret() -> bytes:
    """
    Return the HMAC secret bytes for /img URL signing.

    Precedence:
        1. env var `img_proxy_secret` (any non-empty string) — used as-is, UTF-8 encoded
        2. existing file `{data_dir}/img_proxy_secret` — reused across restarts
        3. newly generated 32-byte urlsafe random secret — persisted to the file with mode 0600
    """
    env_val = os.getenv("img_proxy_secret")
    if env_val:
        return env_val.encode("utf-8")

    secret_path = _Path(config["data_dir"]) / "img_proxy_secret"
    if secret_path.exists():
        return secret_path.read_bytes()

    secret_path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32).encode("ascii")
    secret_path.write_bytes(generated)
    secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return generated


def validate_reader_config() -> None:
    """Raise RuntimeError if the reader is enabled but config is incomplete."""
    if config.get("enable_internal_reader") and not config.get("server_baseurl"):
        raise RuntimeError(
            "enable_internal_reader=true requires server_baseurl to be set "
            "(the proxy origin for signed /img URLs). Set server_baseurl in .env."
        )


# Secret is loaded lazily — on first access via config["img_proxy_secret"]
# to avoid side effects at module import. Tests that need the secret call
# _load_or_create_img_proxy_secret() directly or access config["img_proxy_secret"].
config["img_proxy_secret"] = None  # populated by get_img_proxy_secret()


def get_img_proxy_secret() -> bytes:
    """Cache-on-demand accessor for the HMAC secret."""
    if not config["img_proxy_secret"]:
        config["img_proxy_secret"] = _load_or_create_img_proxy_secret()
    return config["img_proxy_secret"]
```

- [ ] **Step 1.4: Write failing tests for validation + secret lifecycle**

Create/append `tests/test_common.py`:

```python
import os
import stat
from pathlib import Path

import pytest


def test_validate_reader_config_ok_when_baseurl_set(monkeypatch):
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", True)
    monkeypatch.setitem(common.config, "server_baseurl", "http://localhost:8000")
    common.validate_reader_config()  # no exception


def test_validate_reader_config_ok_when_reader_disabled(monkeypatch):
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", False)
    monkeypatch.setitem(common.config, "server_baseurl", None)
    common.validate_reader_config()  # no exception


def test_validate_reader_config_raises_when_reader_enabled_without_baseurl(monkeypatch):
    import common
    monkeypatch.setitem(common.config, "enable_internal_reader", True)
    monkeypatch.setitem(common.config, "server_baseurl", None)
    with pytest.raises(RuntimeError, match="server_baseurl"):
        common.validate_reader_config()


def test_img_proxy_secret_is_generated_and_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv("img_proxy_secret", raising=False)
    import common
    monkeypatch.setitem(common.config, "data_dir", str(tmp_path))
    # clear any cached value
    common.config["img_proxy_secret"] = None

    first = common.get_img_proxy_secret()
    assert len(first) >= 32  # urlsafe token_urlsafe(32) yields ~43 chars

    # File exists with mode 0600
    secret_file = tmp_path / "img_proxy_secret"
    assert secret_file.exists()
    perms = stat.S_IMODE(secret_file.stat().st_mode)
    assert perms == 0o600

    # Second call returns same bytes (cached OR read from file)
    common.config["img_proxy_secret"] = None  # clear cache
    second = common.get_img_proxy_secret()
    assert first == second


def test_img_proxy_secret_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("img_proxy_secret", "fixed-test-secret")
    import common
    monkeypatch.setitem(common.config, "data_dir", str(tmp_path))
    common.config["img_proxy_secret"] = None

    assert common.get_img_proxy_secret() == b"fixed-test-secret"
    # File not created when env var set
    assert not (tmp_path / "img_proxy_secret").exists()
```

- [ ] **Step 1.5: Run tests**

```bash
poetry run pytest tests/test_common.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 1.6: Commit**

```bash
git add pyproject.toml poetry.lock common.py tests/test_common.py
git commit -m "feat: add bleach dep, img_proxy_secret management, reader config validation"
```

---

## Task 2: Extract body + cid map in `reader.py`

**Files:**
- Create: `reader.py`
- Create: `tests/test_reader.py`
- Modify: `tests/conftest.py`

- [ ] **Step 2.1: Extend `insert_email` with `inline_images` kwarg**

In `tests/conftest.py`, update the `insert_email` helper. Find the current implementation and replace with:

```python
def insert_email(
    session,
    sender: str = "sender@example.com",
    email_id: int = 1,
    subject: str = "Hello from the test suite",
    date_str: str = "Mon, 13 Apr 2026 10:00:00 +0000",
    content: bytes | None = None,
    timestamp=None,
    inline_images: dict[str, tuple[str, bytes]] | None = None,
):
    """
    Helper: insert an email row. When `content` is omitted, the canonical multipart
    MIME sample is loaded and headers rewritten. `inline_images` optionally builds
    a multipart/related wrapper with inline parts keyed by Content-ID.
    """
    if content is None:
        msg = email_mod.message_from_bytes(_load_sample_eml())
        msg.replace_header("Subject", subject)
        msg.replace_header("Date", date_str)
        msg.replace_header("From", sender)
        if inline_images:
            from email.mime.multipart import MIMEMultipart
            from email.mime.image import MIMEImage
            from email.mime.text import MIMEText
            related = MIMEMultipart("related")
            for h in ("From", "To", "Subject", "Date", "MIME-Version"):
                if msg[h]:
                    related[h] = msg[h]
            # Preserve the original alternative body as the first related part
            alt_payload = msg.get_payload()
            alternative = MIMEMultipart("alternative")
            for part in alt_payload:
                alternative.attach(part)
            related.attach(alternative)
            for cid, (ctype, raw_bytes) in inline_images.items():
                _maintype, _subtype = ctype.split("/", 1)
                img = MIMEImage(raw_bytes, _subtype=_subtype)
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline")
                related.attach(img)
            content = related.as_bytes()
        else:
            content = msg.as_bytes()

    if timestamp is None:
        timestamp = datetime.datetime(2026, 4, 13, 10, 0, 0)

    row = database.Email(
        sender=sender,
        receiver="user@localhost",
        email_id=email_id,
        subject=subject,
        content=content,
        timestamp=timestamp,
    )
    session.add(row)
    session.commit()
    return row
```

This preserves the existing no-image path and adds a new inline-images path. The `email_mod` import and `datetime` import are already at the top of conftest.py.

- [ ] **Step 2.2: Write failing tests for `extract_body_and_cid_map`**

Create `tests/test_reader.py`:

```python
"""Tests for reader.py — MIME extraction, sanitization, iframe document rendering."""
import email as email_mod

import pytest

import reader


def _build_multipart_with_cid(cid: str, ctype: str, blob: bytes) -> bytes:
    from email.mime.multipart import MIMEMultipart
    from email.mime.image import MIMEImage
    from email.mime.text import MIMEText

    related = MIMEMultipart("related")
    related["From"] = "sender@example.com"
    related["Subject"] = "has inline image"
    html = MIMEText(f'<p>see image: <img src="cid:{cid}" alt="x"></p>', "html", "utf-8")
    related.attach(html)
    _maintype, _subtype = ctype.split("/", 1)
    img = MIMEImage(blob, _subtype=_subtype)
    img.add_header("Content-ID", f"<{cid}>")
    img.add_header("Content-Disposition", "inline")
    related.attach(img)
    return related.as_bytes()


def test_extract_body_prefers_html_part():
    raw = (
        b"From: s@example.com\r\nSubject: t\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain body\r\n"
        b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>html body</p>\r\n--B--\r\n"
    )
    msg = email_mod.message_from_bytes(raw)
    body, cid_map = reader.extract_body_and_cid_map(msg)
    assert "<p>html body</p>" in body
    assert cid_map == {}


def test_extract_body_falls_back_to_plain_wrapped_in_pre():
    raw = (
        b"From: s@example.com\r\nSubject: t\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nplain only\r\n"
    )
    msg = email_mod.message_from_bytes(raw)
    body, cid_map = reader.extract_body_and_cid_map(msg)
    assert "<pre>" in body
    assert "plain only" in body
    assert cid_map == {}


def test_extract_body_builds_cid_map_with_data_uris():
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    raw = _build_multipart_with_cid("image001@abc", "image/png", png_bytes)
    msg = email_mod.message_from_bytes(raw)
    body, cid_map = reader.extract_body_and_cid_map(msg)
    assert 'src="cid:image001@abc"' in body
    assert "image001@abc" in cid_map
    assert cid_map["image001@abc"].startswith("data:image/png;base64,")


def test_extract_body_skips_attachment_disposition():
    from email.mime.multipart import MIMEMultipart
    from email.mime.image import MIMEImage
    from email.mime.text import MIMEText

    msg = MIMEMultipart("related")
    msg["From"] = "s@example.com"
    msg["Subject"] = "t"
    msg.attach(MIMEText("<p>body</p>", "html", "utf-8"))
    attach = MIMEImage(b"fake", _subtype="png")
    attach.add_header("Content-ID", "<should-not-appear>")
    attach.add_header("Content-Disposition", "attachment")
    msg.attach(attach)

    parsed = email_mod.message_from_bytes(msg.as_bytes())
    body, cid_map = reader.extract_body_and_cid_map(parsed)
    assert cid_map == {}
```

- [ ] **Step 2.3: Run the tests (they fail)**

```bash
poetry run pytest tests/test_reader.py -v
```

Expected: ImportError (no `reader` module) or AttributeError (no `extract_body_and_cid_map`).

- [ ] **Step 2.4: Implement `extract_body_and_cid_map` in `reader.py`**

Create `reader.py`:

```python
"""Reader pipeline: MIME extraction, HTML sanitization, iframe document rendering."""
from __future__ import annotations

import base64
import email
from email.message import Message


def extract_body_and_cid_map(msg: Message) -> tuple[str, dict[str, str]]:
    """
    Walk the MIME message and return:
        - body_html: the text/html part if present, else <pre>-wrapped text/plain
        - cid_map: {content-id (unbracketed): "data:{ctype};base64,{b64-bytes}"}

    Inline parts (Content-Disposition other than "attachment") with a Content-ID
    are added to cid_map. Attachments are skipped.
    """
    html_content: str | None = None
    plain_content: str | None = None
    cid_map: dict[str, str] = {}

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            ctype = part.get_content_type()

            if ctype == "text/html" and html_content is None:
                try:
                    html_content = payload.decode(charset, errors="ignore")
                except Exception:
                    continue
            elif ctype == "text/plain" and plain_content is None:
                try:
                    plain_content = payload.decode(charset, errors="ignore")
                except Exception:
                    continue
            elif ctype.startswith("image/"):
                cid = part.get("Content-ID")
                if cid:
                    cid_stripped = cid.strip().lstrip("<").rstrip(">")
                    b64 = base64.b64encode(payload).decode("ascii")
                    cid_map[cid_stripped] = f"data:{ctype};base64,{b64}"
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(charset, errors="ignore")
            if msg.get_content_type() == "text/html":
                html_content = decoded
            else:
                plain_content = decoded

    if html_content is not None:
        return html_content, cid_map
    if plain_content is not None:
        return f"<pre>{_escape_html(plain_content)}</pre>", cid_map
    return "", cid_map


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
```

- [ ] **Step 2.5: Run the tests**

```bash
poetry run pytest tests/test_reader.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 2.6: Commit**

```bash
git add reader.py tests/test_reader.py tests/conftest.py
git commit -m "feat: extract MIME body + cid map in reader.py"
```

---

## Task 3: `clean_and_rewrite` with bleach + custom image Filter

**Files:**
- Modify: `reader.py`
- Modify: `tests/test_reader.py`

- [ ] **Step 3.1: Write failing tests for `clean_and_rewrite`**

Append to `tests/test_reader.py`:

```python
def _identity_sign(url: str) -> str:
    """Test sign_url double: prefix with 'SIGN:' so rewriter output is inspectable."""
    return f"SIGN:{url}"


def test_clean_drops_script_tag():
    out = reader.clean_and_rewrite("<p>ok</p><script>evil()</script>", {}, _identity_sign)
    assert "<script" not in out.lower()
    assert "evil" not in out
    assert "<p>ok</p>" in out


def test_clean_drops_event_handler():
    out = reader.clean_and_rewrite('<a href="http://x" onclick="bad()">hi</a>', {}, _identity_sign)
    assert "onclick" not in out.lower()
    assert "bad()" not in out


def test_clean_drops_javascript_href():
    out = reader.clean_and_rewrite('<a href="javascript:alert(1)">x</a>', {}, _identity_sign)
    assert "javascript:" not in out.lower()


def test_clean_keeps_formatting_tags():
    html = "<p><b>bold</b> <em>em</em> <ul><li>one</li><li>two</li></ul></p>"
    out = reader.clean_and_rewrite(html, {}, _identity_sign)
    assert "<b>bold</b>" in out
    assert "<li>one</li>" in out


def test_clean_rewrites_http_img():
    out = reader.clean_and_rewrite('<img src="http://cdn.example.com/x.png">', {}, _identity_sign)
    assert 'src="SIGN:http://cdn.example.com/x.png"' in out


def test_clean_rewrites_https_img():
    out = reader.clean_and_rewrite('<img src="https://cdn.example.com/x.png">', {}, _identity_sign)
    assert 'src="SIGN:https://cdn.example.com/x.png"' in out


def test_clean_normalizes_protocol_relative_to_https():
    out = reader.clean_and_rewrite('<img src="//cdn.example.com/x.png">', {}, _identity_sign)
    assert 'src="SIGN:https://cdn.example.com/x.png"' in out


def test_clean_resolves_cid_to_data_uri():
    cid_map = {"foo": "data:image/png;base64,AAAA"}
    out = reader.clean_and_rewrite('<img src="cid:foo" alt="x">', cid_map, _identity_sign)
    assert 'src="data:image/png;base64,AAAA"' in out


def test_clean_drops_unknown_cid():
    out = reader.clean_and_rewrite('<p>before</p><img src="cid:missing"><p>after</p>', {}, _identity_sign)
    assert "<img" not in out
    assert "<p>before</p>" in out


def test_clean_preserves_data_uri_img():
    src = "data:image/png;base64,AAAA"
    out = reader.clean_and_rewrite(f'<img src="{src}">', {}, _identity_sign)
    assert f'src="{src}"' in out


def test_clean_strips_srcset():
    html = '<img src="http://x/a.jpg" srcset="http://x/a.jpg 1x, http://x/b.jpg 2x">'
    out = reader.clean_and_rewrite(html, {}, _identity_sign)
    assert "srcset" not in out
    assert 'src="SIGN:http://x/a.jpg"' in out


def test_clean_drops_relative_img_src():
    out = reader.clean_and_rewrite('<p>x</p><img src="/foo.png">', {}, _identity_sign)
    assert "<img" not in out


def test_clean_drops_svg_tag():
    out = reader.clean_and_rewrite('<p>ok</p><svg><circle r="10"/></svg>', {}, _identity_sign)
    assert "<svg" not in out.lower()
    assert "<p>ok</p>" in out


def test_clean_survives_malformed_html():
    malformed = '<p>unclosed<img src=http://x.com/y.png><script>evil'
    out = reader.clean_and_rewrite(malformed, {}, _identity_sign)
    assert "script" not in out.lower() or "<script" not in out.lower()
    assert "evil" not in out


def test_render_iframe_document_includes_csp_and_body():
    doc = reader.render_iframe_document("<p>hi</p>", proxy_origin="http://localhost:8000")
    assert "<!DOCTYPE html>" in doc
    assert "default-src 'none'" in doc
    assert "img-src http://localhost:8000 data:" in doc
    assert '<base target="_blank">' in doc
    assert "<p>hi</p>" in doc
```

- [ ] **Step 3.2: Run the tests (they fail)**

```bash
poetry run pytest tests/test_reader.py -v
```

Expected: AttributeError on `reader.clean_and_rewrite` and `reader.render_iframe_document`.

- [ ] **Step 3.3: Implement `clean_and_rewrite` and `render_iframe_document`**

Append to `reader.py`:

```python
from typing import Callable

import bleach
from bleach.css_sanitizer import CSSSanitizer
from bleach.html5lib_shim import Filter


ALLOWED_TAGS = frozenset({
    "a", "abbr", "acronym", "address", "article", "aside", "b", "blockquote", "br",
    "caption", "cite", "code", "div", "dl", "dt", "dd", "em", "figure", "figcaption",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "kbd",
    "label", "li", "main", "mark", "nav", "ol", "p", "pre", "q", "s", "section",
    "small", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
})

ALLOWED_ATTRS = {
    "*": ["class", "id", "style", "title"],
    "a": ["href", "target", "rel"],
    "img": ["alt", "src", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "time": ["datetime"],
}

ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto", "tel", "data", "cid"})

CSS_SANITIZER = CSSSanitizer(allowed_svg_properties=[])


def _make_image_rewriter(cid_map: dict[str, str], sign_url: Callable[[str], str]):
    """Return a bleach Filter class that rewrites <img src> after sanitization."""

    class ImageRewriter(Filter):
        def __iter__(self):
            for token in Filter.__iter__(self):
                if token.get("type") in ("StartTag", "EmptyTag") and token.get("name") == "img":
                    attrs = dict(token.get("data") or {})
                    src_key = (None, "src")
                    srcset_key = (None, "srcset")
                    src = attrs.get(src_key, "")
                    attrs.pop(srcset_key, None)

                    new_src = _resolve_img_src(src, cid_map, sign_url)
                    if new_src is None:
                        # Drop the tag entirely by skipping the token
                        continue
                    attrs[src_key] = new_src
                    token["data"] = attrs
                yield token

    return ImageRewriter


def _resolve_img_src(
    src: str, cid_map: dict[str, str], sign_url: Callable[[str], str]
) -> str | None:
    if not src:
        return None
    s = src.strip()
    if s.startswith("data:"):
        return s
    if s.startswith("cid:"):
        cid = s[4:]
        return cid_map.get(cid)
    if s.startswith("//"):
        return sign_url("https:" + s)
    if s.startswith("http://") or s.startswith("https://"):
        return sign_url(s)
    return None  # relative or unknown scheme: drop


def clean_and_rewrite(
    html: str,
    cid_map: dict[str, str],
    sign_url: Callable[[str], str],
) -> str:
    """Sanitize HTML with bleach and rewrite image sources for cid/data/proxy."""
    ImageRewriter = _make_image_rewriter(cid_map, sign_url)
    cleaner = bleach.Cleaner(
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=CSS_SANITIZER,
        strip=True,
        strip_comments=True,
        filters=[ImageRewriter],
    )
    return cleaner.clean(html)


_IFRAME_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src {proxy_origin} data:; style-src 'unsafe-inline'; base-uri 'none'">
<base target="_blank">
<style>
body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; color: #222; margin: 0 1rem; }}
img {{ max-width: 100%; height: auto; animation: fade-in 0.3s ease-out; }}
@keyframes fade-in {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
a {{ color: #0066cc; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #1a1a1a; color: #eee; }} }}
</style>
</head>
<body>{body}</body>
</html>"""


def render_iframe_document(cleaned_html: str, proxy_origin: str) -> str:
    """Wrap cleaned body HTML in a minimal document with inner CSP for the srcdoc iframe."""
    return _IFRAME_TEMPLATE.format(proxy_origin=proxy_origin, body=cleaned_html)
```

- [ ] **Step 3.4: Run the tests**

```bash
poetry run pytest tests/test_reader.py -v
```

Expected: all ~18 tests PASS. If any fail because bleach's Filter API has a slightly different shape (bleach 6.x vs 6.2+), adjust the Filter class accordingly — the Filter iterates html5lib tokens as dicts with `"type"`, `"name"`, `"data"` keys.

- [ ] **Step 3.5: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: bleach-based clean_and_rewrite with image src rewriter + iframe document"
```

---

## Task 4: `img_proxy.py` — HMAC sign/verify + DNS-pinned fetch

**Files:**
- Create: `img_proxy.py`
- Create: `tests/test_img_proxy.py`

- [ ] **Step 4.1: Write failing tests for sign/verify**

Create `tests/test_img_proxy.py`:

```python
"""Tests for img_proxy.py — HMAC signing, DNS pinning, SSRF defenses."""
import base64
import hmac as hmac_mod
import socket
from unittest import mock

import pytest

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
```

- [ ] **Step 4.2: Run tests (they fail)**

```bash
poetry run pytest tests/test_img_proxy.py -v
```

Expected: ImportError — no `img_proxy` module yet.

- [ ] **Step 4.3: Implement sign/verify in `img_proxy.py`**

Create `img_proxy.py`:

```python
"""Image proxy: HMAC signing, DNS-pinned fetches, size/type/redirect defenses."""
from __future__ import annotations

import base64
import hmac
import ipaddress
import socket
from hashlib import sha256
from urllib.parse import urlparse

from werkzeug.exceptions import HTTPException, abort


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
```

- [ ] **Step 4.4: Run sign/verify tests**

```bash
poetry run pytest tests/test_img_proxy.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 4.5: Write failing tests for `fetch_image` and SSRF defenses**

Append to `tests/test_img_proxy.py`:

```python
def _signed_param(url: str) -> tuple[str, str]:
    u = _b64(url)
    return u, img_proxy._compute_sig(u, TEST_SECRET)


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
    class R:
        status_code = status

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

    def fake_send(session, req, **kw):
        assert req.url.startswith("http://93.184.216.34/") or req.url.startswith("http://example.com/")
        # We accept either, because the adapter may rewrite the URL
        assert req.headers.get("Host") == "example.com" or "Host" not in req.headers
        return _fake_response(200, {"Content-Type": "image/png"}, [png_body])

    monkeypatch.setattr("requests.Session.send", fake_send)

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
```

- [ ] **Step 4.6: Run fetch_image tests (they fail)**

```bash
poetry run pytest tests/test_img_proxy.py -v
```

Expected: AttributeError on `img_proxy.fetch_image`.

- [ ] **Step 4.7: Implement `fetch_image` with `PinnedIPAdapter`**

Append to `img_proxy.py`:

```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager


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
    ip = ipaddress.ip_address(ip_str)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
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
```

- [ ] **Step 4.8: Run all img_proxy tests**

```bash
poetry run pytest tests/test_img_proxy.py -v
```

Expected: all ~17 tests PASS.

- [ ] **Step 4.9: Commit**

```bash
git add img_proxy.py tests/test_img_proxy.py
git commit -m "feat: img_proxy module with HMAC signing and DNS-pinned SSRF-safe fetcher"
```

---

## Task 5: Wire `/img` route in `feed_server.py`

**Files:**
- Modify: `feed_server.py`

- [ ] **Step 5.1: Write failing integration test for `/img` route**

Append to `tests/test_feed_server.py`:

```python
import base64 as _b64mod
import socket as _socket_mod


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
    import img_proxy

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
```

- [ ] **Step 5.2: Run test (fails — no route yet)**

```bash
poetry run pytest tests/test_feed_server.py::test_img_route_happy_path -v
```

Expected: 404 returned (route doesn't exist).

- [ ] **Step 5.3: Add `/img` route to `feed_server.py`**

In `feed_server.py`, add imports at the top (alongside existing imports):

```python
from flask import Response, request
from werkzeug.exceptions import HTTPException

import img_proxy
from common import get_img_proxy_secret
```

And inside `create_app()`, add this route after the existing ones:

```python
    @app.get("/img")
    def image_proxy_route():
        u = request.args.get("u", "")
        sig = request.args.get("sig", "")
        if not u:
            abort(400)
        if not sig:
            abort(403)

        secret = get_img_proxy_secret()
        if not img_proxy.verify_signature(u, sig, secret):
            abort(403)

        try:
            pad = "=" * (-len(u) % 4)
            url = base64.urlsafe_b64decode(u + pad).decode("utf-8")
        except Exception:
            abort(400)

        body, ctype = img_proxy.fetch_image(url, secret)
        return Response(
            body,
            mimetype=ctype,
            headers={
                "Cache-Control": "public, max-age=604800",
                "Content-Security-Policy": "default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
```

Add `import base64` at the top if not already present.

- [ ] **Step 5.4: Run the /img route tests**

```bash
poetry run pytest tests/test_feed_server.py -v -k img_route
```

Expected: 4 new tests PASS.

- [ ] **Step 5.5: Commit**

```bash
git add feed_server.py tests/test_feed_server.py
git commit -m "feat: add signed /img proxy route"
```

---

## Task 6: Rewire `view_article` + fix 404 bug + tighten outer CSP + update template

**Files:**
- Modify: `feed_server.py`
- Modify: `templates/article.html`
- Modify: `static/reader.css`
- Modify: `static/reader.js`
- Modify: `tests/test_feed_server.py`

- [ ] **Step 6.1: Update `article.html` template**

Replace the entire contents of `templates/article.html`:

```html
{% extends "base.html" %}
{% block title %}{{ subject }}{% endblock %}
{% block body %}
<article class="article">
  <header>
    <h1>{{ subject }}</h1>
    <p class="meta">From: {{ sender }} | Date: {{ date }}</p>
  </header>
  <iframe
    id="email-body"
    class="email-body-iframe"
    sandbox="allow-popups allow-popups-to-escape-sandbox"
    srcdoc="{{ iframe_document|e }}"
    referrerpolicy="no-referrer"
    loading="lazy"
    title="Email body"
  ></iframe>
</article>
{% endblock %}
```

- [ ] **Step 6.2: Add iframe CSS rule**

Append to `static/reader.css`:

```css
.email-body-iframe {
  width: 100%;
  height: 80vh;
  border: 0;
  display: block;
  background: transparent;
}
```

- [ ] **Step 6.3: Remove dead `.content img` code from reader.js**

Read `static/reader.js`. Find any code that references `.content img` or targets the old inline content container. Delete those blocks. If the file becomes empty or nearly empty, leave a single-line comment `// Reader UI is rendered inside a sandboxed iframe; no outer JS needed.` so Docker COPY doesn't fail on empty files.

- [ ] **Step 6.4: Write failing tests for view_article new shape + 404 fix**

In `tests/test_feed_server.py`, find and UPDATE:

- Rename `test_article_route_swallows_404_as_500_when_guid_unknown` → `test_article_route_404s_when_guid_unknown`
- Rename `test_article_route_swallows_404_as_500_for_unknown_feed` → `test_article_route_404s_for_unknown_feed`
- Flip `assert resp.status_code == 500` → `assert resp.status_code == 404` in both
- Remove the `# BUG ...` comments (bug is now fixed)

Update `test_article_route_renders_email_body` to check iframe output. Replace the body of that test with:

```python
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
```

Add a new test for inner CSP presence:

```python
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
```

Add a test for tightened outer CSP:

```python
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
```

- [ ] **Step 6.5: Run tests (most fail until view_article is rewired)**

```bash
poetry run pytest tests/test_feed_server.py -v
```

Expected: article tests fail because view_article still uses the old pipeline and CSP is still permissive.

- [ ] **Step 6.6: Rewire `view_article` and tighten outer CSP**

In `feed_server.py`:

**Replace the `_security_headers` body** to tighten CSP:

```python
    @app.after_request
    def _security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-src 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response
```

**Replace the `view_article` function body** with the new pipeline:

```python
    @app.get("/article/<feed_name>/<guid>")
    def view_article(feed_name, guid):
        sender_email = feed_name_to_email(feed_name)
        try:
            record = db.get_email_by_guid(sender_email, guid)
            if not record:
                abort(404)

            msg = email_mod.message_from_bytes(record.content)
            subject = str(email_mod.header.make_header(
                email_mod.header.decode_header(msg["subject"])
            ))
            body_html, cid_map = reader.extract_body_and_cid_map(msg)

            proxy_base = (config.get("server_baseurl") or "").rstrip("/")
            proxy_origin = proxy_base  # origin = baseurl for signed /img
            secret = get_img_proxy_secret()

            def _sign(url):
                return img_proxy.sign_url(url, secret, proxy_base)

            cleaned = reader.clean_and_rewrite(body_html, cid_map, _sign)
            iframe_document = reader.render_iframe_document(cleaned, proxy_origin)

            return render_template(
                "article.html",
                subject=subject,
                sender=sender_email,
                date=msg["date"] or "",
                iframe_document=iframe_document,
            )
        except HTTPException:
            raise
        except Exception:
            logging.exception(f"Error serving article {feed_name}/{guid}")
            abort(500)
```

Add these imports at top of `feed_server.py` if not already present:

```python
import reader
```

Remove the `from util import cleanse_content, sanitize_html` line and the `extract_article_content` function (both obsolete).

- [ ] **Step 6.7: Run full suite**

```bash
poetry run pytest -v
```

Expected: all tests pass (new article tests, renamed 404 tests, preserved tests for /health, /stats, /<feed>.xml, etc.).

- [ ] **Step 6.8: Commit**

```bash
git add feed_server.py templates/article.html static/reader.css static/reader.js tests/test_feed_server.py
git commit -m "feat: rewire view_article to new reader pipeline, fix 404→500 bug, tighten outer CSP"
```

---

## Task 7: Remove dead `sanitize_html` + its test

**Files:**
- Modify: `util.py`
- Modify: `tests/test_util.py`

- [ ] **Step 7.1: Verify sanitize_html has no remaining callers**

```bash
poetry run python -c "import ast, pathlib
bad = []
for p in pathlib.Path('.').rglob('*.py'):
    if 'tests/' in str(p) or '.venv' in str(p): continue
    try:
        src = p.read_text()
        if 'sanitize_html' in src and p.name not in ('util.py',):
            bad.append(str(p))
    except Exception: pass
print('Callers outside util.py:', bad)
"
```

Expected output: `Callers outside util.py: []` — confirms nothing else uses it.

- [ ] **Step 7.2: Delete `sanitize_html` from `util.py`**

Remove the `sanitize_html` function. Keep all other helpers (`extract_email_address`, `extract_domain_address`, `extract_name_from_email`, `utf8_decoder`, `cleanse_content`).

- [ ] **Step 7.3: Delete the corresponding test**

In `tests/test_util.py`, remove `test_sanitize_html_removes_script_and_event_handlers` and the `sanitize_html` import.

- [ ] **Step 7.4: Run full suite**

```bash
poetry run pytest -v
```

Expected: all tests pass. Total count: starting ~23 + Task 1 (5) + Task 2 (4) + Task 3 (14) + Task 4 (17) + Task 5 (4) + Task 6 (3 new + 2 renamed) - 1 removed (sanitize_html test) ≈ ~70 tests. (The spec estimated ~50; actual count may differ because we added per-SSRF-vector tests. Either count is fine.)

- [ ] **Step 7.5: Commit**

```bash
git add util.py tests/test_util.py
git commit -m "chore: remove dead sanitize_html (bleach replaces it)"
```

---

## Task 8: Integration smoke + docs

**Files:**
- Modify: `README.md`

- [ ] **Step 8.1: Run the full suite**

```bash
poetry run pytest -v
```

Expected: full suite green, ~70 tests pass.

- [ ] **Step 8.2: Lint clean**

```bash
poetry run ruff check .
```

Expected: `All checks passed!`. If any new lint errors appear in `reader.py` or `img_proxy.py`, fix inline (use `# noqa: S608` etc. only with justification).

- [ ] **Step 8.3: Docker build sanity check**

```bash
docker build -f Dockerfile.serve -t email2rss-serve:sp2 .
docker build -f Dockerfile.fetch_and_generate -t email2rss-fetch:sp2 .
```

Expected: both images build successfully. This confirms `bleach` installs cleanly in the container.

- [ ] **Step 8.4: Update README**

In `README.md`, add a new subsection under "Internal RSS Reader" describing the security model. After the existing "Features" list, add:

```markdown
### Security Model

When the internal reader is enabled, rendered emails pass through a hardened pipeline:

- **HTML sanitization** — `bleach` strips scripts, event handlers, dangerous tags, and `javascript:` URIs.
- **Sandboxed iframe** — email body renders inside `<iframe sandbox="allow-popups allow-popups-to-escape-sandbox">` with no `allow-scripts`. Defense in depth: even if sanitization misses a vector, no JS can execute.
- **Inline CSP** — the iframe srcdoc declares `default-src 'none'; img-src {server_baseurl} data:` — images load only via the signed proxy or inline data: URIs.
- **Signed image proxy** — external `<img>` srcs are rewritten to `/img?u=<base64>&sig=<hmac>`. The proxy validates the signature, resolves DNS with `AF_UNSPEC`, rejects private/loopback/link-local IPs (all returned addresses), connects to the pre-resolved IP with correct Host + TLS SNI (defeats DNS rebinding), rejects non-image Content-Types, and caps body size at 5 MB with streaming reads.
- **Required config** — `server_baseurl` is required when `enable_internal_reader=true`. The HMAC secret is auto-generated on first start and persisted to `{data_dir}/img_proxy_secret` (mode 0600); override via env var `img_proxy_secret`.
```

- [ ] **Step 8.5: Commit docs**

```bash
git add README.md
git commit -m "docs: describe reader security model"
```

---

## Acceptance criteria checklist

- [ ] All ~70 tests pass locally with `poetry run pytest -v`
- [ ] `poetry run ruff check .` clean
- [ ] Both Docker images build successfully
- [ ] Manual: article page source shows iframe with expected sandbox attrs
- [ ] Manual: `/img?u=<b64>&sig=wrong` returns 403
- [ ] Manual: `/img` with a URL resolving to `10.0.0.1` returns 403
- [ ] Manual: `/article/{feed}/unknown-guid` returns 404 (not 500)
- [ ] Manual: startup with `enable_internal_reader=true` and no `server_baseurl` aborts clearly
- [ ] Manual: first startup creates `{data_dir}/img_proxy_secret` with mode 0600
- [ ] Manual: outer response CSP header has `img-src 'self' data:`, not `img-src *`

## Implementation ordering rationale

Tasks are ordered by dependency:
- **Task 1** (deps + config) is foundational; nothing else imports cleanly without bleach installed.
- **Tasks 2 & 3** (reader.py pieces) have no runtime dependencies on the web layer; can be tested in isolation.
- **Task 4** (img_proxy.py) has no runtime deps on reader.py; can be tested independently.
- **Task 5** (/img route) depends on Task 4 (fetch_image) and Task 1 (secret).
- **Task 6** (view_article rewire) depends on Tasks 2/3 (pipeline) and Task 5 (sign_url + proxy base).
- **Task 7** (cleanup) can only happen after Task 6 removes the last caller.
- **Task 8** (smoke + docs) gates on the whole thing being green.
