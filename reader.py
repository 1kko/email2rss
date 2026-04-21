"""Reader pipeline: MIME extraction, HTML sanitization, iframe document rendering."""
from __future__ import annotations

import base64
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
                except Exception:  # noqa: S112
                    continue
            elif ctype == "text/plain" and plain_content is None:
                try:
                    plain_content = payload.decode(charset, errors="ignore")
                except Exception:  # noqa: S112
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
