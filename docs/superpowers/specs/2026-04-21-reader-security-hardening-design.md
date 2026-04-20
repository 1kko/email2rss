# Reader Security Hardening (revised after Codex review)

**Status:** Approved design, ready for implementation plan
**Date:** 2026-04-21
**Scope:** Sub-project 2 of 4 in the broader reliability/feature improvement initiative

## Purpose

Harden the internal reader against two classes of problem:

1. **XSS / DOM injection from email HTML.** Emails are untrusted content. If the current regex `sanitize_html` misses a vector, malicious JavaScript executes in the reader's origin.
2. **Privacy leakage via external resources.** External `<img>` fetches expose the reader's IP, cache state, and viewing behavior to arbitrary senders.

Also fix a correctness bug in `feed_server.view_article` that converts intended 404s into 500s (characterized in sub-project 1).

## Non-goals

- Click-tracking-link rewriting. Users click at their own risk.
- Cookie scrubbing — iframe sandbox already blocks cookie access for rendered email.
- Multi-user support. Still a single-user reader.
- Hardening toggle. The hardened behavior is the only behavior.

## Architecture

### Trust model

- **Outer page (`article.html`)** — trusted, runs on host origin. Renders the sandboxed iframe and a `postMessage`-free resize listener.
- **Inner iframe (`srcdoc`)** — untrusted, opaque origin, **no `allow-scripts`**. Even if an attacker bypasses the sanitizer, there is no JS context to run in.
- **Image proxy (`/img`)** — trusted endpoint. Signed URLs only. Strict DNS/IP validation, pinned connection, streaming fetch with size cap.

### Sandbox choice

`<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc="..." referrerpolicy="no-referrer">`

- `allow-popups` — clicks on `<a target="_blank">` open new tabs.
- `allow-popups-to-escape-sandbox` — the new tab is a normal tab.
- **No `allow-scripts`** — simplest and safest. The tradeoff is that we cannot `postMessage` a computed content height from inside the iframe. We solve this by giving the iframe a fixed-height scroll container (see Template below). This is a deliberate UX↔security tradeoff: mild double-scroll behavior in exchange for zero script surface area.
- Everything else blocked: no forms, no same-origin, no top-level navigation, no plugins.

### Content pipeline

```
  email bytes
     │
     ▼
  email.message_from_bytes()
     │
     ▼
  extract_body_and_cid_map(msg) ──────► body_html (raw), cid_map: {cid_id: data_uri}
     │
     ▼
  clean_and_rewrite(body_html, cid_map, sign_url)
   (bleach.Cleaner with a custom Filter)
     │           ├─ sanitize dangerous tags/attrs/CSS
     │           ├─ resolve <img src="cid:X"> ↔ cid_map → data: URI (drop tag if unknown)
     │           └─ rewrite <img src="http(s)://..."> → {server_baseurl}/img?u=<b64>&sig=<hmac>
     ▼
  render_iframe_document(cleaned_html)
     │           wraps in minimal HTML doc with inner CSP meta tag
     ▼
  <iframe srcdoc="..."> in article.html
```

### `extract_body_and_cid_map(msg) -> (str, dict[str, str])`

Walks the MIME parts. For each inline part with a `Content-ID`:
- Strip the angle brackets from the cid value (`<abc@xyz>` → `abc@xyz`).
- Base64-encode the part's bytes.
- Build `"data:{content-type};base64,{b64-bytes}"`.
- Store under the cid key.

Returns `(body_html, cid_map)`. `body_html` is the text/html part (preferred) or `<pre>{plain}</pre>` fallback.

Skips parts marked `Content-Disposition: attachment`. Attachments are not exposed to the reader.

### `clean_and_rewrite(html, cid_map, sign_url) -> str`

Uses `bleach.Cleaner` with:

- **Tag allowlist**: `{a, abbr, acronym, address, article, aside, b, blockquote, br, caption, cite, code, div, dl, dt, dd, em, figure, figcaption, footer, h1..h6, header, hr, i, img, kbd, label, li, main, mark, nav, ol, p, pre, q, s, section, small, span, strike, strong, sub, summary, sup, table, tbody, td, tfoot, th, thead, time, tr, u, ul, video}`. No `script`, `style`, `iframe`, `object`, `embed`, `form`, `input`, `base`, `meta`, `link`, `svg`.
- **Attribute allowlist**: `{class, id, style, title, alt, src, href, width, height, colspan, rowspan, target, cite, datetime}` — scoped per-tag by bleach.
- **CSS allowlist**: `bleach.css_sanitizer.CSSSanitizer` with standard properties. URL values inside `style` (e.g. `background: url(...)`) are rejected except `data:` — bleach handles this with its CSS sanitizer.
- **Protocol allowlist for `href`**: `{http, https, mailto, tel}`. No `javascript:`, `data:`, `vbscript:`.
- **Custom Filter** (runs after bleach's standard cleaning) that walks start-tag tokens for `<img>`:
  - If `src` missing or empty → drop tag.
  - If `src` starts with `data:` → keep.
  - If `src` starts with `cid:` → look up the id in `cid_map`; replace `src` with the mapped data URI; if not found, drop the tag.
  - If `src` starts with `//` → normalize to `https:` + the rest.
  - If `src` starts with `http://` or `https://` → call `sign_url(src)` and replace `src` with the signed proxy URL.
  - Else (relative URL or unknown scheme) → drop the tag.
  - Strip `srcset` attribute entirely (we don't proxy responsive image sets; the browser falls back to `src`).

`sign_url(url) -> str` returns `"{server_baseurl}/img?u={b64(url)}&sig={hmac}"` where `hmac = HMAC-SHA256(IMG_PROXY_SECRET, b64(url)).hexdigest()[:32]`. Truncation to 32 hex chars (128 bits) is sufficient for abuse prevention.

### `IMG_PROXY_SECRET` management

Added to `common.config`:

```python
"img_proxy_secret": os.getenv("img_proxy_secret"),
```

**If unset at startup AND `enable_internal_reader=true`:**
- Generate a 32-byte urlsafe random secret with `secrets.token_urlsafe(32)`.
- Persist to `{data_dir}/img_proxy_secret` with mode 0600.
- Load on subsequent startups.

This means the secret survives restarts within a data directory but never appears in env files or logs. Explicit `img_proxy_secret=` env var overrides the file.

Also add at startup: if `enable_internal_reader=true` and `server_baseurl` is unset, **abort with a clear error** ("server_baseurl must be set when internal reader is enabled"). This removes the Host-header fallback from the earlier draft.

### `/img` proxy route

`GET /img?u=<b64url-of-url>&sig=<hex-hmac>`

```
1. Compute expected_sig = HMAC-SHA256(secret, u).hexdigest()[:32]
   If hmac.compare_digest(expected_sig, sig) is False → 403.
2. Base64url-decode u. On failure → 400.
3. Parse URL. Scheme must be http or https. Else → 400.
4. resolved_ips = [item[4][0] for item in socket.getaddrinfo(host, port, family=AF_UNSPEC, type=SOCK_STREAM)]
   For each ip in resolved_ips:
     If ipaddress.ip_address(ip).is_private / .is_loopback / .is_link_local / .is_reserved / .is_multicast → 403.
5. Pick one validated IP (first). Build a urllib3-based connection to that specific IP with Host header set to the original hostname, and TLS SNI set to the hostname for https. This prevents any further DNS lookup and defeats DNS rebinding.
6. Request with allow_redirects=False, timeout=5s. On connection error → 502.
7. Response status != 200 → 502.
8. Content-Type (stripped of params) not in {image/png, image/jpeg, image/gif, image/webp} → 415. SVG is excluded deliberately.
9. Stream the response in 8 KB chunks, accumulating into a bytearray. If total exceeds 5 MB before EOF → 413.
10. Return 200 with:
    - Content-Type: (original, from allowlist)
    - Cache-Control: public, max-age=604800
    - Content-Security-Policy: default-src 'none'
    - X-Content-Type-Options: nosniff
    - Referrer-Policy: no-referrer
```

**Pinned-IP connection with correct TLS SNI (step 5-6)**: implemented by subclassing `requests.adapters.HTTPAdapter` — override `init_poolmanager` and `get_connection_with_tls_context` to route to the pre-resolved IP while preserving the hostname for SNI and `Host` header. Lives in a new module `img_proxy.py` alongside the fetch helper (`fetch_image(url, secret) -> (bytes, content_type) | HTTPException`). The Flask route `/img` in `feed_server.py` is a thin wrapper that calls `fetch_image` and maps its result to a Flask response. ~60 lines total in `img_proxy.py`.

### Template changes

**`templates/article.html`** — replaces the current inline content block with a fixed-height sandboxed iframe:

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

`iframe_document` is the full minimal HTML doc built by `render_iframe_document`:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; img-src {proxy_origin} data:; style-src 'unsafe-inline'; base-uri 'none'">
  <base target="_blank">
  <style>
    body { font: 16px/1.5 -apple-system, system-ui, sans-serif; color: #222; margin: 0 1rem; }
    img { max-width: 100%; height: auto; animation: fade-in 0.3s ease-out; }
    @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
    a { color: #0066cc; }
    @media (prefers-color-scheme: dark) { body { background: #1a1a1a; color: #eee; } }
  </style>
</head>
<body>{{ cleaned_html }}</body>
</html>
```

Key choices:
- `base-uri 'none'` — even though we set `<base target="_blank">`, this also prevents any script (if one ever slipped through) from changing the base URI.
- `<base>` has only `target` — `rel` is not a valid `<base>` attribute.
- `img-src {proxy_origin} data:` where `proxy_origin` is the scheme+host+port from `server_baseurl` (verified non-empty at startup).
- Inline `<style>` in srcdoc provides the minimal CSS that used to cascade from the outer page (typography, fade-in animation, dark-mode fallback, responsive images).

**`static/reader.css`** — add:

```css
.email-body-iframe {
  width: 100%;
  height: 80vh;
  border: 0;
  display: block;
  background: transparent;
}
```

**`static/reader.js`** — remove the existing `.content img` fade-in code (the iframe's inner CSS handles it). No replacement needed; the reader needs no JS for the hardened article view.

### `view_article` 404→500 fix

In `feed_server.py`:

```python
from werkzeug.exceptions import HTTPException

@app.get("/article/<feed_name>/<guid>")
def view_article(feed_name, guid):
    sender_email = feed_name_to_email(feed_name)
    try:
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        msg = email_mod.message_from_bytes(record.content)
        subject = str(email_mod.header.make_header(email_mod.header.decode_header(msg["subject"])))
        body_html, cid_map = extract_body_and_cid_map(msg)
        cleaned = clean_and_rewrite(body_html, cid_map, sign_url=_sign_image_url)
        iframe_document = render_iframe_document(cleaned)
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

### Outer page CSP tightening

Currently `img-src * data:`. After this change, the outer page no longer renders external images directly (they live inside the sandboxed iframe). Outer CSP tightens to:

```
default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-src 'self'
```

## Dependencies

- **`bleach>=6.2,<7`** — new. HTML sanitization + the Filter API for image rewriting. ~5 transitive deps (html5lib, six, webencodings).
- **`requests>=2.33.0`** — already pinned after CVE-2026-25645.

No other new deps. `urllib3` is already transitive via `requests`.

## Testing

### New tests in `tests/test_reader_sanitizer.py` (new file)

Covers the new `clean_and_rewrite` function. `tests/test_util.py` keeps its existing non-reader tests (`extract_email_address`, `extract_domain_address`, `cleanse_content`).

- `test_clean_and_rewrite_drops_script_tag`
- `test_clean_and_rewrite_drops_event_handler_attribute`
- `test_clean_and_rewrite_drops_javascript_href`
- `test_clean_and_rewrite_drops_style_url_with_javascript`
- `test_clean_and_rewrite_keeps_safe_formatting` — bold, lists, tables, links survive
- `test_clean_and_rewrite_rewrites_http_img` — asserts src becomes `/img?u=...&sig=...`, sig verifies
- `test_clean_and_rewrite_rewrites_https_img`
- `test_clean_and_rewrite_normalizes_protocol_relative_to_https` — `//cdn/x.png` becomes `https://cdn/x.png` then signed
- `test_clean_and_rewrite_resolves_cid_to_data_uri` — `<img src="cid:foo">` with `cid_map={"foo": "data:image/png;base64,..."}` yields the data URI
- `test_clean_and_rewrite_drops_unknown_cid` — `<img src="cid:missing">` with empty cid_map drops the tag
- `test_clean_and_rewrite_strips_srcset` — `<img src="a.jpg" srcset="b.jpg 2x">` emits rewritten src, no srcset
- `test_clean_and_rewrite_drops_relative_img` — `<img src="/foo.png">` is dropped (not in our allowlist of schemes)
- `test_clean_and_rewrite_drops_svg_tag`
- `test_clean_and_rewrite_survives_malformed_html` — unclosed tags, unquoted attrs, embedded `<` in attrs

### New tests in `tests/test_img_proxy.py`

Monkeypatch `requests.Session.send` (via the adapter) to inject fake responses. Verify the proxy's decisions:

- `test_img_proxy_rejects_bad_signature` — mutate the sig → 403
- `test_img_proxy_rejects_missing_signature`
- `test_img_proxy_rejects_non_base64_url` → 400
- `test_img_proxy_rejects_non_http_scheme` — `file:///etc/passwd` → 400
- `test_img_proxy_rejects_private_ipv4` — monkeypatch getaddrinfo to return `10.0.0.1` → 403
- `test_img_proxy_rejects_loopback_ipv4` — `127.0.0.1`
- `test_img_proxy_rejects_link_local_ipv4` — `169.254.169.254` (AWS metadata)
- `test_img_proxy_rejects_private_ipv6` — `fc00::1`
- `test_img_proxy_rejects_when_any_resolved_ip_is_private` — getaddrinfo returns mixed public+private → 403 (reject if ANY is private)
- `test_img_proxy_fetches_png_happy_path` — mock returns PNG bytes + `image/png` → 200, correct content-type, cache header present
- `test_img_proxy_rejects_html_content_type` — 415
- `test_img_proxy_rejects_svg` — `image/svg+xml` → 415
- `test_img_proxy_rejects_oversized` — mock streams 6 MB → 413 without fully buffering
- `test_img_proxy_does_not_follow_redirects` — mock returns 302 → 502
- `test_img_proxy_timeouts_become_502` — mock raises Timeout → 502
- `test_img_proxy_pinned_ip_defeats_rebinding` — asserts the HTTP Host header equals the original hostname while the adapter connects to the resolved IP (behavior verification of the pinned-IP transport)

### New tests in `tests/test_feed_server.py`

- `test_article_route_renders_body_in_sandboxed_iframe` — response contains `<iframe ... sandbox="allow-popups allow-popups-to-escape-sandbox"` and a `srcdoc` attribute
- `test_article_route_inner_csp_present` — srcdoc string contains the expected inner CSP with `default-src 'none'`
- `test_article_route_outer_csp_tightened` — response headers show the tightened outer CSP (no `img-src *`)
- `test_article_route_404s_when_guid_unknown` — renamed, assertion flipped from 500 to 404
- `test_article_route_404s_for_unknown_feed` — renamed, assertion flipped
- `test_article_route_renders_cid_image_as_data_uri` — email with inline cid part + `<img src="cid:X">` → srcdoc contains the data URI
- `test_img_proxy_secret_is_stable_across_restarts` — write secret file, restart app simulation, secret unchanged

### Updates to existing tests

- Remove `test_sanitize_html_removes_script_and_event_handlers` (function removed). Replaced by `test_clean_and_rewrite_drops_*` set above.
- Update `test_article_route_renders_email_body` — check iframe presence and srcdoc containing subject text, not outer HTML
- `tests/conftest.py` — extend `insert_email` with optional `inline_images={cid_id: (content_type, bytes)}` so tests can build multipart MIMEs with inline parts

### Removed code

- `util.sanitize_html` — dead after bleach replaces it. Delete function and its tests.

### Total impact

Starting: 23 tests. New: ~25. Updated: 3. Removed: 1. Final: ~50 tests.

## Acceptance criteria

1. All ~50 tests pass locally and in CI.
2. `bleach` appears in `pyproject.toml` and `poetry.lock`.
3. Manual: article page source shows iframe with exact sandbox attrs and srcdoc containing inner CSP.
4. Manual: a test newsletter with remote images, an inline cid: image, and a known-malformed script renders: the cid image appears, the remote images load via `/img`, no scripts execute (check browser console is clean).
5. Manual: `/img?u=<b64>&sig=wrong` returns 403.
6. Manual: `/img?u=<b64-encoding-of-http://10.0.0.1/x.png>&sig=<valid>` returns 403.
7. Manual: `/article/{feed}/unknown-guid` returns 404 (not 500).
8. Manual: startup with `enable_internal_reader=true` and no `server_baseurl` aborts with a clear error.
9. Manual: on first startup with no `img_proxy_secret`, a file `{data_dir}/img_proxy_secret` is created with mode 0600.

## Out of scope (explicitly deferred)

- `allow-scripts` inside the sandbox (would enable iframe auto-height via `postMessage`). Requires a nonce-based inner CSP; the tradeoff wasn't worth it for this iteration. Revisit if the fixed-height UX is painful.
- On-disk image cache for repeat loads. Browser cache handles the common case.
- Per-article "view original HTML" bypass. If users want an escape hatch, add later.
- CSS `@import`, `@font-face`, and web-font support — bleach's CSS sanitizer strips these; newsletters with custom fonts will fall back to system fonts.
- Link tracking-URL rewriting (stripping UTM params, unshortening). Could layer on top of `href` values in the Filter if desired later.
- Input validation on `feed_name` beyond existing behavior.
- Migration story for users who had older unsigned `/img` URLs bookmarked — not a concern since this route is new.

## Known tradeoffs

- **Fixed-height iframe (80vh) means long emails scroll internally.** Conscious choice over adding `allow-scripts`. If users hate it, revisit with a nonce-based height reporter in a follow-up.
- **No SVG, no cid that's external, no `srcset`.** Narrow image support in exchange for narrow attack surface.
- **`bleach` adds ~5 transitive deps** (html5lib, six, webencodings, and two more). Acceptable for the XSS defense it provides.
- **First-run UX requires `server_baseurl` to be set** when the internal reader is enabled. Startup aborts otherwise. Explicit > Host-header guessing.
