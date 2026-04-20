# Reader Security Hardening

**Status:** Approved design, ready for implementation plan
**Date:** 2026-04-21
**Scope:** Sub-project 2 of 4 in the broader reliability/feature improvement initiative

## Purpose

Harden the internal reader against two classes of problem:

1. **XSS / DOM injection from email HTML.** Emails are untrusted content. Today the reader pre-sanitizes HTML with `util.sanitize_html` and renders it inline in the article page. If the sanitizer misses a vector, malicious JavaScript executes in the reader's origin.
2. **Privacy leakage via external resources.** External `<img>` fetches expose the reader's IP address and viewing behavior to arbitrary senders, including tracking pixels disguised as images. Cache-timing side channels further fingerprint the reader.

This sub-project also fixes a correctness bug in `feed_server.view_article` that converts intended 404s into 500s (characterized in sub-project 1's test suite).

## Non-goals

- Read-receipt blocking via link click tracking. Users click external links voluntarily; that's outside the sanitizer's job.
- Cookie scrubbing or third-party cookie management — the iframe sandbox already prevents cookie access for the rendered email.
- Multi-user account isolation. This remains a single-user reader.
- Feature flag / toggle to disable hardening. The hardened behavior is the only behavior.

## Architecture

### Content pipeline

```
  email bytes
     │
     ▼
  email.message_from_bytes()
     │
     ▼
  extract_body_html(msg)               # existing in feed_server.py
     │
     ▼
  cleanse_content()                    # existing; strips invalid XML chars
     │
     ▼
  sanitize_html()                      # existing; strips script/style/event handlers
     │
     ▼
  rewrite_image_srcs()                 # NEW: rewrite external <img> to /img proxy
     │
     ▼
  render_iframe_document()             # NEW: wrap in minimal HTML doc with inner CSP
     │
     ▼
  <iframe srcdoc="...">                # NEW: sandboxed iframe in article.html
```

### Trust boundaries

- **Outer page (article.html):** Trusted. Host origin. Runs the iframe auto-size JS.
- **Inner iframe (srcdoc):** Untrusted. Opaque origin due to sandbox. Runs email HTML.
- **Image proxy (/img):** Trusted endpoint. Fetches arbitrary URLs but only on behalf of the email's embedded image tags after they've been rewritten through the sanitizer.

### Iframe sandbox

`<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc="..." referrerpolicy="no-referrer">`

- `allow-popups` — clicks on `<a target="_blank">` open new tabs
- `allow-popups-to-escape-sandbox` — the new tab is a normal tab, not sandboxed
- (Everything else blocked): no scripts, no forms, no same-origin access, no top-level navigation, no plugins, no autofill, no downloads
- `referrerpolicy="no-referrer"` on the iframe — stops Referer leakage on permitted fetches

Inside the srcdoc document:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; img-src {proxy_origin} data: cid:; style-src 'unsafe-inline'">
  <base target="_blank" rel="noopener noreferrer">
</head>
<body>{body_html}</body>
</html>
```

- `default-src 'none'` — nothing loads unless explicitly allowed
- `img-src {proxy_origin} data: cid:` — images only from the server's own proxy endpoint, `data:` URIs (inline), and `cid:` refs (multipart inline parts). `{proxy_origin}` is the origin (scheme + host + optional port) of `config.server_baseurl`; if that's unset or invalid, fall back to the `Host` header of the current request.
- `style-src 'unsafe-inline'` — inline `style=""` attributes allowed so email formatting renders
- `<base target="_blank">` — every `<a href>` opens a new tab, making `allow-popups` actually useful

### Image proxy endpoint

`GET /img?u=<urlsafe-base64-encoded-URL>`

Encoded URL is decoded and validated. The proxy fetches with strict constraints and re-serves the bytes.

**Validation pipeline:**

1. Base64 decode failure → 400
2. Scheme not in `{http, https}` → 400
3. Hostname DNS-resolves to a private, loopback, link-local, or reserved IP → 403
4. Fetch with `allow_redirects=False`, `timeout=5s` → on connection error, 502
5. Response status != 200 → 502 (this covers 3xx redirects, 4xx/5xx upstream errors; returning the raw status would pass 3xx to the browser which would then follow it, defeating the point of `allow_redirects=False`)
6. Response Content-Type (stripped of parameters) not in `{image/png, image/jpeg, image/gif, image/webp, image/svg+xml}` → 415
7. Response body > 5 MB → 413
8. Else: 200 with original Content-Type, `Cache-Control: public, max-age=604800`, `Content-Security-Policy: default-src 'none'`, `X-Content-Type-Options: nosniff`

**SSRF defenses:**

- Scheme allowlist blocks `file://`, `gopher://`, `ftp://`
- IP allowlist-by-exclusion blocks `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` (including AWS/Azure metadata), `::1`, etc.
- No redirect following blocks redirect-based SSRF
- Content-Type allowlist prevents proxying HTML, JS, or opaque bytes
- Size cap bounds memory use per request
- No outbound auth headers, custom User-Agent for traceability

### `rewrite_image_srcs` (in `util.py`)

Signature: `def rewrite_image_srcs(html: str, proxy_base: str = "/img") -> str`

Regex finds every `<img ... src="..." ...>` and, if the src starts with `http://`, `https://`, or `//`, replaces it with `f'{proxy_base}?u={base64.urlsafe_b64encode(src.encode()).decode()}'`. Leaves `data:` and `cid:` sources untouched. Leaves relative URLs untouched (unlikely in email but harmless). Preserves all other tag attributes (alt, width, height, style).

### `view_article` 404 fix (in `feed_server.py`)

The existing `try/except Exception` wrapper around `view_article` catches `werkzeug.exceptions.HTTPException` (the base class of `NotFound`), turning intended 404s into 500s. Fix:

```python
from werkzeug.exceptions import HTTPException

# inside view_article:
try:
    ...
except HTTPException:
    raise
except Exception:
    logging.exception(...)
    abort(500)
```

`HTTPException` is re-raised so Flask can convert it to the intended response; only non-HTTP exceptions hit the 500 branch.

## Template changes

### `templates/article.html`

Replace the current inline content block:

```html
  <div class="content">
    {# content is pre-sanitized via util.sanitize_html #}
    {{ content|safe }}
  </div>
```

With:

```html
  <iframe
    id="email-body"
    sandbox="allow-popups allow-popups-to-escape-sandbox"
    srcdoc="{{ iframe_document|e }}"
    referrerpolicy="no-referrer"
    loading="lazy"
  ></iframe>
```

The view passes `iframe_document` (the full minimal HTML doc built by `render_iframe_document`) instead of raw `content`.

### `static/reader.js`

Auto-size the iframe on load. Append:

```js
const iframe = document.getElementById('email-body');
if (iframe) {
  iframe.addEventListener('load', () => {
    const doc = iframe.contentDocument;
    if (doc && doc.body) {
      iframe.style.height = doc.body.scrollHeight + 20 + 'px';
    }
  });
}
```

Reading `contentDocument.body.scrollHeight` is permitted by the outer page even with the chosen sandbox flags (the restriction is on the iframe's code acting on the parent, not vice versa).

### `static/reader.css`

```css
#email-body { width: 100%; border: 0; display: block; }
```

## Testing

### New tests in `tests/test_util.py`

- `test_rewrite_image_srcs_rewrites_http` — `<img src="http://x/y.png">` rewrites, base64 decodes back to original URL
- `test_rewrite_image_srcs_rewrites_https` — same for `https://`
- `test_rewrite_image_srcs_rewrites_protocol_relative` — `//cdn.example.com/x.png` gets rewritten (treat as https-equivalent)
- `test_rewrite_image_srcs_leaves_data_uri_alone` — `<img src="data:image/png;base64,...">` unchanged
- `test_rewrite_image_srcs_leaves_cid_alone` — `<img src="cid:abc@xyz">` unchanged
- `test_rewrite_image_srcs_preserves_other_attributes` — alt/width/height survive

### New tests in `tests/test_feed_server.py`

- `test_article_route_renders_body_in_sandboxed_iframe` — response contains `<iframe` with correct sandbox attribute and a srcdoc
- `test_article_route_inner_csp_present_in_srcdoc` — srcdoc string contains the expected meta CSP with `default-src 'none'`
- `test_img_proxy_rejects_non_base64` — `?u=not-base64!` → 400
- `test_img_proxy_rejects_non_http_scheme` — base64-encoded `file:///etc/passwd` → 400
- `test_img_proxy_rejects_private_ip` — URL pointing at `http://10.0.0.1/x.png` → 403 (monkeypatch `socket.gethostbyname`)
- `test_img_proxy_fetches_image` — monkeypatch `requests.get` to return PNG bytes + `image/png`; assert 200, Content-Type, cache header
- `test_img_proxy_rejects_html_content_type` — monkeypatch returns `text/html` → 415
- `test_img_proxy_rejects_oversized` — monkeypatch returns 6 MB body → 413
- `test_img_proxy_does_not_follow_redirects` — monkeypatch returns 302 → 502 (redirects are converted so the browser can't follow them)

### Updated tests

- `test_article_route_swallows_404_as_500_when_guid_unknown` → renamed to `test_article_route_404s_when_guid_unknown`, assertion flipped from 500 to 404
- `test_article_route_swallows_404_as_500_for_unknown_feed` → renamed to `test_article_route_404s_for_unknown_feed`, assertion flipped
- `test_article_route_renders_email_body` — assertion updated to check for iframe + srcdoc containing the subject, not top-level HTML containing subject

### Total impact

Starting: 23 tests. New: ~13. Updated: 3. Final: ~36 tests.

## Dependencies

- `requests` — already pinned (`>=2.33.0`) after the CVE-2026-25645 fix. Used for the `/img` proxy.

No new third-party dependencies.

## Acceptance criteria

1. All ~36 tests pass locally and in CI.
2. Manual: open an article in the reader that contains external images. Images render correctly via `/img?u=...`. View-source confirms iframe + srcdoc + inner CSP.
3. Manual: open an article containing a tracking pixel pointing at `http://10.0.0.1/p.gif` (simulated). The proxy returns 403; browser shows a broken-image icon.
4. Manual: inspect the outer page's response headers — the outer CSP no longer needs `img-src *`; can be tightened to `default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:`.
5. Manual: visit `/article/{feed}/unknown-guid` — browser shows 404 (not 500).
6. Manual: verify the iframe auto-sizes correctly for a long email (no double scrollbar).

## Out of scope (explicitly deferred)

- Per-user "load images" toggle (sub-project 4's reader feature work, or never)
- Redirect following with re-validation (adds complexity without new threat model wins — if an email wants to redirect through a tracker, blocking is fine)
- Image caching to disk (current design relies on browser cache; server-side cache is a future optimization)
- Detecting and stripping 1×1 tracker pixels heuristically — not needed once every image is proxied
- Hardening `feed_server`'s other routes (`/`, `/stats`, `/<feed>.xml`, etc.) — these serve server-controlled content, no untrusted HTML renders there
