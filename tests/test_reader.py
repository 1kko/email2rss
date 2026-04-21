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
