"""Tests for reader.py — MIME extraction, sanitization, iframe document rendering."""
import email as email_mod

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


def _identity_sign(url: str) -> str:
    """Test sign_url double: prefix with 'SIGN:' so rewriter output is inspectable."""
    return f"SIGN:{url}"


def test_clean_drops_script_tag():
    """bleach strip=True removes <script> tags but keeps the inner text as inert
    text nodes. That's safe inside our sandboxed iframe (default-src 'none',
    no allow-scripts) but we pin the behavior here so a regression that lets the
    <script> element itself leak back would fail."""
    out = reader.clean_and_rewrite("<p>ok</p><script>evil()</script>", {}, _identity_sign)
    assert "<script" not in out.lower()
    assert "</script" not in out.lower()
    # The text "evil()" survives as inert text; that is bleach's documented behavior.
    assert "evil()" in out
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


def test_clean_preserves_style_tag_contents():
    """Newsletter <style> blocks must survive (so layout CSS applies inside the
    sandboxed iframe, not leaked as visible text). Defense-in-depth against
    malicious CSS comes from the iframe's inner CSP: default-src 'none' plus
    img-src {proxy_origin} data: blocks every CSS-initiated external fetch
    (background-image URLs, @import, @font-face, etc.)."""
    html = (
        '<style>@media (max-width: 640px) { .col { width: 100% !important; } }</style>'
        '<p>body</p>'
    )
    out = reader.clean_and_rewrite(html, {}, _identity_sign)
    # <style> tag survives (so CSS applies in-iframe, not leaked as inert text)
    assert "<style>" in out.lower() or "<style " in out.lower()
    # Safe CSS rules preserved
    assert "@media" in out
    assert "max-width" in out
    # Body content preserved
    assert "<p>body</p>" in out


def test_style_tag_css_is_not_rendered_as_visible_text():
    """Regression guard for the production bug where <style> blocks were being
    stripped by bleach, leaving their CSS rules as visible text in the reader."""
    css_rule = "body { color: red; }"
    out = reader.clean_and_rewrite(f"<style>{css_rule}</style><p>hi</p>", {}, _identity_sign)
    # The <style> tag wraps the CSS, so there's no text node containing just the CSS
    # outside a <style> tag. Simple assertion: the <style> element is present.
    assert "<style" in out.lower()
    # And the output between </style> and <p>hi</p> does not contain the CSS rule
    # (if bleach had stripped <style>, the CSS text would appear bare).
    import re
    after_style = re.search(r'</style>(.*)$', out, re.DOTALL)
    assert after_style is not None
    assert css_rule not in after_style.group(1)


def test_clean_survives_malformed_html():
    """Unclosed tags, bare attr values — bleach + html5lib normalize and strip
    the dangerous <script>. Inner text may survive as inert text (see note in
    test_clean_drops_script_tag)."""
    malformed = '<p>unclosed<img src=http://x.com/y.png><script>evil'
    out = reader.clean_and_rewrite(malformed, {}, _identity_sign)
    assert "<script" not in out.lower()
    assert "</script" not in out.lower()


def test_clean_drops_data_uri_href():
    """data: URIs are not in ALLOWED_PROTOCOLS; href is stripped from the anchor."""
    out = reader.clean_and_rewrite(
        '<a href="data:text/html,<script>xss</script>">click</a>', {}, _identity_sign
    )
    assert "data:text/html" not in out
    assert "click" in out  # link text preserved even if href stripped


def test_clean_drops_cid_href():
    """cid: URIs on anchors are not in ALLOWED_PROTOCOLS."""
    out = reader.clean_and_rewrite('<a href="cid:foo">click</a>', {}, _identity_sign)
    assert "cid:foo" not in out
    assert "click" in out


def test_clean_handles_empty_string():
    assert reader.clean_and_rewrite("", {}, _identity_sign) == ""


def test_clean_handles_uppercase_http_scheme():
    """Scheme matching is case-insensitive per RFC 3986."""
    out = reader.clean_and_rewrite('<img src="HTTPS://cdn.example.com/x.png">', {}, _identity_sign)
    assert 'src="SIGN:HTTPS://cdn.example.com/x.png"' in out


def test_render_iframe_document_includes_csp_and_body():
    doc = reader.render_iframe_document("<p>hi</p>", proxy_origin="http://localhost:8000")
    assert "<!DOCTYPE html>" in doc
    assert "default-src 'none'" in doc
    assert "img-src http://localhost:8000 data:" in doc
    assert '<base target="_blank">' in doc
    assert "<p>hi</p>" in doc


def _make_html_msg(html_body: str, sender="s@example.com"):
    """Build a simple text/html MIME message for tests."""
    import email as email_mod
    raw = (
        f"From: {sender}\r\n"
        f"To: me@localhost\r\n"
        f"Subject: test\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n\r\n"
        f"{html_body}\r\n"
    ).encode("utf-8")
    return email_mod.message_from_bytes(raw)


def test_extract_preview_image_picks_first_large_image():
    msg = _make_html_msg(
        '<p>hi</p>'
        '<img src="http://cdn.example.com/hero.jpg" width="600" height="400">'
        '<img src="http://cdn.example.com/later.jpg" width="400" height="300">'
    )
    assert reader.extract_preview_image(msg) == "http://cdn.example.com/hero.jpg"


def test_extract_preview_image_skips_1x1_tracking_pixel():
    msg = _make_html_msg(
        '<img src="http://track.example.com/open.gif" width="1" height="1">'
        '<img src="http://cdn.example.com/real.jpg" width="600" height="400">'
    )
    assert reader.extract_preview_image(msg) == "http://cdn.example.com/real.jpg"


def test_extract_preview_image_skips_tracking_filename_hints():
    msg = _make_html_msg(
        '<img src="http://x.com/pixel.gif" width="600" height="1">'
        '<img src="http://x.com/tracking-beacon.png">'
        '<img src="http://x.com/hero.png" width="600" height="400">'
    )
    assert reader.extract_preview_image(msg) == "http://x.com/hero.png"


def test_extract_preview_image_skips_cid_and_data():
    msg = _make_html_msg(
        '<img src="cid:foo123" width="600" height="400">'
        '<img src="data:image/png;base64,AAAA" width="600" height="400">'
        '<img src="https://x.com/hero.jpg" width="600" height="400">'
    )
    assert reader.extract_preview_image(msg) == "https://x.com/hero.jpg"


def test_extract_preview_image_normalizes_protocol_relative_to_https():
    msg = _make_html_msg('<img src="//cdn.example.com/h.png" width="600" height="400">')
    assert reader.extract_preview_image(msg) == "https://cdn.example.com/h.png"


def test_extract_preview_image_returns_none_when_no_images():
    msg = _make_html_msg('<p>no images here</p>')
    assert reader.extract_preview_image(msg) is None


def test_extract_preview_image_skips_small_declared_dimensions():
    # Both dims declared and below threshold → skip
    msg = _make_html_msg('<img src="http://x.com/tiny.png" width="20" height="20">')
    assert reader.extract_preview_image(msg) is None


def test_extract_preview_image_accepts_image_without_declared_dimensions():
    # No width/height attributes → we can't verify size, accept it
    msg = _make_html_msg('<img src="http://x.com/maybe.png">')
    assert reader.extract_preview_image(msg) == "http://x.com/maybe.png"
