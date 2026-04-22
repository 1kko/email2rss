"""Reader pipeline: MIME extraction, HTML sanitization, iframe document rendering."""
from __future__ import annotations

import base64
import logging
from email.message import Message
from typing import Callable

import bleach
from bleach.css_sanitizer import CSSSanitizer
from bleach.html5lib_shim import Filter, attr_val_is_uri
from bleach.sanitizer import BleachSanitizerFilter

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
    "small", "span", "strike", "strong", "style", "sub", "summary", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
})
# NOTE: <style> is allowed because email newsletters heavily rely on inline
# <style> blocks for layout (@media queries for mobile, responsive tables).
# Without it, bleach strip=True drops the <style> tag but keeps the CSS rules
# as visible text nodes inside the rendered body. bleach's CSSSanitizer still
# cleans the inner CSS of URL references and dangerous values. Safe inside
# our sandboxed iframe (styles cannot escape to the parent page).

ALLOWED_ATTRS = {
    "*": ["class", "id", "style", "title"],
    "a": ["href", "target", "rel"],
    "img": ["alt", "src", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "time": ["datetime"],
}

ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto", "tel"})

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
    sl = s.lower()
    if sl.startswith("data:"):
        return s
    if sl.startswith("cid:"):
        return cid_map.get(s[4:])
    if s.startswith("//"):
        return sign_url("https:" + s)
    if sl.startswith("http://") or sl.startswith("https://"):
        return sign_url(s)
    return None  # relative or unknown scheme: drop


# attr_val_is_uri without (None, "src"): bleach won't protocol-check img src,
# leaving that entirely to ImageRewriter. This is safe because every other
# src-bearing element (video, audio, iframe, …) is absent from ALLOWED_TAGS
# and is stripped before it reaches the serializer.
_ATTR_VAL_IS_URI_NO_SRC = attr_val_is_uri - {(None, "src")}


class _ImageSrcPassthroughCleaner(bleach.Cleaner):
    """Cleaner that skips bleach's protocol check on src attributes.

    bleach's BleachSanitizerFilter applies the allowed-protocols allowlist to
    every attribute in attr_val_is_uri (which includes "src") uniformly across
    all tags. That strips data: and cid: from <img src> before our ImageRewriter
    filter can handle them.

    By subclassing and injecting a reduced attr_val_is_uri we delegate full
    validation of <img src> to ImageRewriter, which only passes through:
      - data: URIs  (from the cid_map, set by ImageRewriter itself)
      - http:/https: URLs  (proxied via sign_url)
      - protocol-relative //  (promoted to https and proxied)
    All other src values — including relative paths and unknown schemes — are
    dropped by ImageRewriter (the tag is removed entirely).  data:/cid: on
    <a href> remain blocked because "data" and "cid" are absent from
    ALLOWED_PROTOCOLS and href is still in attr_val_is_uri.
    """

    def clean(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError(
                f"argument cannot be of {text.__class__.__name__!r} type, "
                "must be of text type"
            )
        if not text:
            return ""

        dom = self.parser.parseFragment(text)
        filtered = BleachSanitizerFilter(
            source=self.walker(dom),
            allowed_tags=self.tags,
            attributes=self.attributes,
            strip_disallowed_tags=self.strip,
            strip_html_comments=self.strip_comments,
            css_sanitizer=self.css_sanitizer,
            allowed_protocols=self.protocols,
            attr_val_is_uri=_ATTR_VAL_IS_URI_NO_SRC,
        )
        for filter_class in self.filters:
            filtered = filter_class(source=filtered)
        return self.serializer.render(filtered)


def clean_and_rewrite(
    html: str,
    cid_map: dict[str, str],
    sign_url: Callable[[str], str],
) -> str:
    """Sanitize HTML with bleach and rewrite image sources for cid/data/proxy."""
    ImageRewriter = _make_image_rewriter(cid_map, sign_url)
    cleaner = _ImageSrcPassthroughCleaner(
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
@media (prefers-color-scheme: dark) {{
  /* Invert+hue-rotate so inline email styles (color:#000, transparent bg +
     dark text, etc.) flip to their dark-mode counterparts. Our own
     declarations must be "pre-inverted": html painted as #e5e5e5 renders
     as #1a1a1a to the viewer after the filter runs. Setting html to
     #1a1a1a directly would leave the viewer seeing #e5e5e5 — a light
     surface where dark inline text (#212121 → #dedede) sits, making the
     content invisible.
     Images/SVG/video/canvas get a canceling inverse filter so photos,
     logos, and charts render with their original colors. */
  html {{ background: #e5e5e5; filter: invert(1) hue-rotate(180deg); }}
  img, svg, picture, video, canvas {{ filter: invert(1) hue-rotate(180deg); }}
}}
</style>
</head>
<body>{body}</body>
</html>"""


def render_iframe_document(cleaned_html: str, proxy_origin: str) -> str:
    """Wrap cleaned body HTML in a minimal document with inner CSP for the srcdoc iframe."""
    csp = _IFRAME_CSP.format(proxy_origin=proxy_origin)
    return _IFRAME_TEMPLATE.format(csp=csp, body=cleaned_html)


def extract_plain_text(msg) -> str:
    """
    Return HTML-stripped plain text for FTS indexing.

    Prefers text/plain parts; falls back to HTML->plain via bleach.
    Returns empty string if neither part is present.
    """
    body_html, _cid_map = extract_body_and_cid_map(msg)
    if not body_html:
        return ""
    # If body_html is actually plain text wrapped in <pre>, strip the <pre> wrapper
    # by running bleach with no allowed tags — bleach strips all markup.
    return bleach.clean(body_html, tags=[], strip=True)


_TRACKING_FILENAME_HINTS = ("pixel", "track", "open", "beacon", "spacer")
_BRAND_FILENAME_HINTS = ("logo", "brand", "header-image", "footer-image", "masthead", "sig-")
_MIN_IMAGE_PX = 50
_BANNER_ASPECT_RATIO = 3.5  # width/height above this looks like newsletter masthead


def _is_banner_shaped(width: int, height: int) -> bool:
    """
    True when width/height > _BANNER_ASPECT_RATIO. Newsletter masthead banners
    are almost always very wide strips (e.g. 600x100 → 6:1) while real content
    hero images are ≤ 16:9 (~1.78:1). We only flag wide-and-short; tall images
    are portrait photos, not banners.
    """
    if height <= 0:
        return False
    return (width / height) > _BANNER_ASPECT_RATIO


def extract_preview_image(msg) -> str | None:
    """
    Walk the MIME message's HTML body and return the best "usable" <img> URL
    for a landing-page thumbnail, or None.

    Selection strategy:
    1. Collect all <img> candidates that pass the filter rules below.
    2. Among candidates with declared dimensions, prefer non-banner-shaped ones
       (width/height ≤ _BANNER_ASPECT_RATIO). Banner-shaped images are template
       mastheads repeated across every email from a sender — they produce
       identical thumbnails for every article. If any non-banner candidate
       exists, return the largest by area.
    3. If every candidate is banner-shaped, fall back to the largest of those
       (better than no image).
    4. If no candidate has declared dimensions, return the first one (best we
       can do without rendering the email to measure images).

    Filter rules (each applies before a candidate is considered):
    - src starts with http://, https://, or // (protocol-relative → https:)
    - cid: and data: URIs skipped
    - width=1 or height=1 skipped (tracking pixel)
    - filename contains pixel/track/open/beacon/spacer → skipped (trackers)
    - filename contains logo/brand/header-image/footer-image/masthead/sig-
      → skipped (branding imagery)
    - if both width and height are declared, both must be >= 50
    """
    import re

    body_html, _cid_map = extract_body_and_cid_map(msg)
    if not body_html:
        return None

    # (area, width, height, src) — width/height retained for aspect-ratio test
    candidates: list[tuple[int, int, int, str]] = []
    first_unknown_area: str | None = None

    for match in re.finditer(r'<img\b([^>]*)>', body_html, flags=re.IGNORECASE):
        attrs_str = match.group(1)
        src = _attr(attrs_str, "src")
        if not src:
            continue
        src = src.strip()
        lower_src = src.lower()

        if lower_src.startswith("cid:") or lower_src.startswith("data:"):
            continue

        if src.startswith("//"):
            src = "https:" + src
            lower_src = src.lower()

        if not (lower_src.startswith("http://") or lower_src.startswith("https://")):
            continue

        # Filename hint filters — trackers AND branding imagery
        fname = lower_src.rsplit("/", 1)[-1]
        if any(hint in fname for hint in _TRACKING_FILENAME_HINTS):
            continue
        if any(hint in fname for hint in _BRAND_FILENAME_HINTS):
            continue

        width = _attr_int(attrs_str, "width")
        height = _attr_int(attrs_str, "height")

        if width == 1 or height == 1:
            continue

        if width is not None and height is not None:
            if width < _MIN_IMAGE_PX or height < _MIN_IMAGE_PX:
                continue
            candidates.append((width * height, width, height, src))
        else:
            # No declared dimensions — remember the first so we can fall back
            if first_unknown_area is None:
                first_unknown_area = src

    if candidates:
        non_banner = [c for c in candidates if not _is_banner_shaped(c[1], c[2])]
        pool = non_banner if non_banner else candidates
        pool.sort(key=lambda c: c[0], reverse=True)
        return pool[0][3]

    # Fallback: first image with unknown dimensions
    return first_unknown_area


def _attr(attrs_str: str, name: str) -> str | None:
    """Extract an attribute value from an HTML tag's attribute substring."""
    import re
    m = re.search(
        rf'\b{re.escape(name)}\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))',
        attrs_str,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def _attr_int(attrs_str: str, name: str) -> int | None:
    v = _attr(attrs_str, name)
    if v is None:
        return None
    try:
        return int(v.strip().rstrip("px"))
    except ValueError:
        return None
