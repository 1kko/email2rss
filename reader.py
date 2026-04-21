"""Reader pipeline: MIME extraction, HTML sanitization, iframe document rendering."""
from __future__ import annotations

import base64
import logging
from email.message import Message
from typing import Callable

import bleach
from bleach.css_sanitizer import CSSSanitizer
from bleach.html5lib_shim import Filter

logger = logging.getLogger(__name__)


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
            disposition_raw = part.get("Content-Disposition") or ""
            disposition_type = str(disposition_raw).strip().split(";", 1)[0].strip().lower()
            if disposition_type == "attachment":
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
                    logger.warning("failed to decode %s part with charset=%r", ctype, charset)
                    continue
            elif ctype == "text/plain" and plain_content is None:
                try:
                    plain_content = payload.decode(charset, errors="ignore")
                except Exception:  # noqa: S112
                    logger.warning("failed to decode %s part with charset=%r", ctype, charset)
                    continue
            elif ctype.startswith("image/"):
                cid = part.get("Content-ID")
                if cid:
                    cid_stripped = cid.strip().lstrip("<").rstrip(">").strip()
                    if cid_stripped:
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


_IFRAME_CSP = (
    "default-src 'none'; img-src {proxy_origin} data:; style-src 'unsafe-inline'; base-uri 'none'"
)

_IFRAME_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
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
    csp = _IFRAME_CSP.format(proxy_origin=proxy_origin)
    return _IFRAME_TEMPLATE.format(csp=csp, body=cleaned_html)
