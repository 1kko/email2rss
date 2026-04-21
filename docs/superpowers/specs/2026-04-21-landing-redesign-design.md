# Landing Page Redesign (Netflix-style feed browser)

**Status:** Approved design, ready for implementation plan
**Date:** 2026-04-21
**Scope:** Breaking change to `/`. Everything else unchanged.

## Purpose

Replace `/`'s raw XML file listing with a browsable, visually rich interface:

- A **Latest** section at the top showing the 10 most recent articles across all feeds, as portrait cards with sender labels.
- **Per-sender rows** below, each with a horizontal-scrolling strip of article cards for that newsletter.
- Thumbnail per article — hero image from the email HTML if available, domain favicon as fallback, coloured monogram as last resort.
- Keep the plain file list accessible at `/list` so power users can still grab raw XML URLs.

## Non-goals

- Server-side infinite scroll or pagination. Row-level `limit` is hardcoded (Latest=10, per sender=10). Future concern.
- Read/unread filter or time-range filter on the landing. Sorting is fixed: Latest desc, sender rows sorted by each sender's newest article desc, articles within a row sorted newest first.
- Search integration in the cards. Search stays at `/search`.
- Card-level preview on hover, Quick-view modals, keyboard shortcuts. YAGNI.
- Skeleton loading states. Server-rendered, synchronous.
- Bulk actions or selection.

## Routes

| URL | Before | After |
|-----|--------|-------|
| `/` | `index.html` — plain `<ul>` of XML filenames | **New cards-based landing** (details below) |
| `/list` | *(did not exist)* | The old `/` behavior verbatim — `<ul>` of XML filenames linking to `/<feed>.xml` |
| `/<feed>.xml` | RSS XML | **unchanged** |
| `/article`, `/article/<feed>`, `/article/<feed>/<guid>` | Internal reader | **unchanged** |
| `/search`, `/img`, `/health`, `/stats`, `/subscriptions.opml` | | **unchanged** |

Breaking change limited to `/` only. RSS reader clients hitting `/<feed>.xml` directly are unaffected.

## Layout

### Latest section (top)

- Section heading: `✦ Latest` + `Last 10 · See all →` on the right (the "See all" link points at `/article`, the internal reader's all-articles list).
- Horizontal scroll row of **portrait cards** (200 × ~420px, 4:5 thumb).
- Mixed senders, sorted newest first.
- Each card shows: thumbnail, sender label (small favicon + domain), subject (3-line clamp), relative date, unread dot (top-right) if `is_read=false`.
- Separator from the first sender row: 1px bottom border + 2.75rem bottom margin. **No background tint.**

### Per-sender rows

- One row per sender, sorted by the sender's newest article desc.
- Each row heading: domain favicon + full sender email + `N articles · See all →` link (to `/article/<feed>`).
- Horizontal scroll row of **landscape cards** (260 × ~280px, 16:9 thumb).
- Limit 10 cards per row (newest first).
- Each card: thumbnail, subject (2-line clamp), relative date, unread dot (bottom-right) if unread.

### Scroll behaviour (both row types)

- Hidden native scrollbar.
- `scroll-snap-type: x mandatory`, `scroll-snap-align: start`, `scroll-padding-inline: 0.75rem`, `scroll-behavior: smooth`, `overscroll-behavior-x: contain`, `-webkit-overflow-scrolling: touch`.
- Hover-visible circular prev/next arrow buttons (42px) on desktop. Disabled state (opacity 0, pointer-events: none) at each end. Click → `scrollBy` two card widths smooth.
- Left/right edge fade gradients (36–40px wide, fade to `--bg-color`). Opacity toggled by scroll position (`data-at-start` / `data-at-end` attributes on the scroller element).
- Arrows hidden entirely on `@media (hover: none)` — touch devices rely on native swipe.
- Card hover: `translateY(-3px) scale(1.02)` + elevated shadow.

### Duplication policy (confirmed: A)

An article shown in Latest may also appear in its sender's row below. Different visual treatment (portrait vs landscape, tinted sender label vs plain) makes the duplication feel like two views of the same data rather than redundancy. Simpler data model, each row is a complete browsable unit.

## Data model changes

### New column on `Email`

```python
preview_image_url = Column(String, nullable=True)
```

Populated at `save_email` time via a new `reader.extract_preview_image(msg)` helper. `None` if extraction fails or the email has no usable images. Cached per-email so the landing page query is fast.

### Migration

`migrate_database()` extends to:

1. `ALTER TABLE emails ADD COLUMN preview_image_url TEXT` (nullable, default NULL).
2. For existing rows with `preview_image_url IS NULL`, background-backfill via `extract_preview_image` in the same one-time pass the FTS backfill uses (Sub-project 4 pattern). Per-row failures log and continue with `None`.

Safe on both fresh and upgrade paths.

## `reader.extract_preview_image(msg) -> str | None`

Walks the MIME message, parses the HTML body, and returns the first "usable" `<img>` URL.

**"Usable" rules:**

- `src` starts with `http://`, `https://`, or `//` (protocol-relative normalized to `https:`).
- Skip `cid:` and `data:` URIs — hard to re-serve through the proxy from a list page.
- Skip 1×1 pixels: if `width="1"` or `height="1"` attribute present → skip.
- Skip if `src` filename hints at tracking: substring `pixel`, `track`, `open`, `beacon`, `p.gif`, `spacer` (case-insensitive).
- If the `<img>` has `width`/`height` attributes, require both ≥ 50 — small icons skip.
- Otherwise accept.
- If no usable image, return `None`.

Lives in `reader.py` next to `extract_body_and_cid_map` and `extract_plain_text`. Uses bleach's tokenizer (reuse existing html5lib-based parsing) — no new dep.

## Favicon fallback

When `preview_image_url` is `None`, the card uses the Google favicon service:

```
https://www.google.com/s2/favicons?domain=<domain>&sz=128
```

Served through our existing `/img` proxy (HMAC signing, SSRF defense, etc. — full reuse).

## Monogram fallback

Decision tree per card (resolved server-side, rendered as a single branch):

1. If `preview_image_url` is present → render the extracted image (`<img class="article-card__hero">`).
2. Else if a domain can be derived from sender → render the Google favicon (`<img class="article-card__favicon">`, centred at 40px).
3. Else → render the monogram (`<span class="article-card__monogram">{{ letter }}</span>` with inline `background: hsl(...)`).

Monogram:

- Letter: first char of sender's local part, uppercased (`alice@x.com` → `A`). Default `?` if local part is empty.
- Colour: `hsl(hash(sender) % 360, 55%, 50%)` via a small `util.monogram_hue(sender) -> int` helper. Deterministic per sender.

If a favicon `<img>` fails to load at the browser (Google s2 is extremely reliable — 99.9%+ — but network hiccups happen), the user sees a broken-image icon. Accepted edge case; no `onerror` JS handler. Monogram is only the server-side choice when the sender has no extractable domain (rare).

## Relative date helper

New `util.py` function `relative_date(dt: datetime.datetime) -> str` producing Korean-localized strings:

| Delta | Output |
|-------|--------|
| < 60s | `방금 전` |
| < 60m | `{N}분 전` |
| same day | `{N}시간 전` |
| yesterday | `어제` |
| < 7 days | `{N}일 전` |
| < 30 days | `{N}주 전` |
| < 365 days | `{N}개월 전` |
| else | `{N}년 전` |

Accepts both naive and tz-aware datetimes. Returns a string.

## Database helpers (new in `database.py`)

```python
def get_landing_data(latest_limit: int = 10, per_sender_limit: int = 10) -> dict:
    """
    Return the full payload for the landing page.

    Shape:
        {
            "latest": [article_dict, ...],   # up to `latest_limit`
            "rows": [                        # one entry per sender
                {
                    "sender": str,
                    "feed_name": str,        # sanitized (alice_example_com)
                    "favicon_url": str,      # /img-proxied Google s2 URL
                    "monogram_letter": str,
                    "monogram_hue": int,
                    "article_count": int,
                    "articles": [article_dict, ...],  # up to `per_sender_limit`
                },
                ...
            ],
        }
    """

def _article_dict(row: Email, sign_url: callable) -> dict:
    """
    Return a dict for a single article:
        sender, subject, date_str, relative_date, guid, feed_name,
        image_url (signed /img URL, or None), is_read, is_starred,
        sender_favicon_url (signed /img URL), monogram_letter, monogram_hue.
    """
```

- `get_landing_data` runs **two queries**: one for latest (ORDER BY timestamp DESC LIMIT 10), one for per-sender (window function: latest 10 per sender partition).
- All image URLs are run through `img_proxy.sign_url` before return.
- Favicon URL is built per-sender using the domain extracted from the sender email.

## Template structure

### `templates/index.html` — full rewrite

```jinja2
{% extends "base.html" %}
{% block title %}email2rss{% endblock %}
{% block body %}
{% import "_cards.html" as cards %}

<main class="feed-rows">
  {% if not data.latest and not data.rows %}
    <p class="empty">No feeds yet. The IMAP fetcher will populate this page once it pulls in emails.</p>
  {% else %}
    <section class="feed-row latest-section">
      <div class="feed-row__header">
        <h2 class="feed-row__title">Latest</h2>
        <a class="feed-row__see-all" href="/article">Last {{ data.latest|length }} · See all →</a>
      </div>
      {{ cards.scroller(data.latest, portrait=True) }}
    </section>

    {% for row in data.rows %}
      <section class="feed-row">
        <div class="feed-row__header">
          <h2 class="feed-row__title">
            {{ cards.row_favicon(row) }}
            {{ row.sender }}
          </h2>
          <a class="feed-row__see-all" href="/article/{{ row.feed_name }}">
            {{ row.article_count }} articles · See all →
          </a>
        </div>
        {{ cards.scroller(row.articles, portrait=False) }}
      </section>
    {% endfor %}
  {% endif %}
</main>
{% endblock %}
```

### `templates/_cards.html` — Jinja2 macros

- `scroller(articles, portrait)` — wraps `<div class="feed-row__scroller">` with prev/next buttons, edge gradients, and the scroll track populated with article cards.
- `portrait_card(article)` — portrait variant (Latest section).
- `landscape_card(article)` — landscape variant (sender rows).
- `row_favicon(row)` — `<img>` or monogram `<span>` depending on whether favicon URL is available.

### `templates/list.html` — new (old `/` content, moved)

```jinja2
{% extends "base.html" %}
{% block title %}email2rss — XML feed list{% endblock %}
{% block body %}
<h1>RSS Feeds (XML)</h1>
<p>Direct XML URLs for RSS readers. For the styled landing, <a href="/">go back to /</a>.</p>
<ul>
  {% for name in entries %}
    <li><a href="/{{ name }}">{{ name }}</a></li>
  {% endfor %}
</ul>
{% endblock %}
```

## CSS (new in `static/reader.css`, ~180 lines)

Key classes with their contracts:

- `.feed-rows` — max-width container, horizontal padding
- `.feed-row` — one section (Latest or per-sender)
- `.feed-row__header` — flex row: title left, See-all right
- `.feed-row__title` — h2 with optional favicon and emoji accent (for Latest)
- `.feed-row__scroller` — positioned relative; hosts arrows + edge fades + track
- `.feed-row__scroller::before`/`::after` — left/right gradient fades, opacity driven by `data-at-start`/`data-at-end`
- `.feed-row__arrow` — circular 42px button, absolute-positioned, visible on scroller hover
- `.feed-row__arrow--prev`/`--next` — positioning
- `.feed-row__track` — horizontal flex with snap, hidden scrollbar, smooth scroll
- `.article-card` — landscape card (260px flex-basis)
- `.article-card--portrait` — portrait variant (200px, 4:5 thumb)
- `.article-card__thumb` — aspect ratio container for the thumbnail
- `.article-card__favicon` — 40px when falling back to favicon
- `.article-card__monogram` — letter block with HSL background
- `.article-card__body` — padding + gap for internal elements
- `.article-card__sender-label` — inline favicon + domain, portrait only
- `.article-card__subject` — clamped (2 lines landscape / 3 lines portrait)
- `.article-card__date` — meta-coloured small text
- `.article-card__unread-dot` — 8px (landscape, bottom-right) / 10px (portrait, top-right) blue indicator

Latest section separation:

- `.latest-section` — `padding-bottom: 1.5rem; border-bottom: 1px solid var(--border); margin-bottom: 2.75rem`.
- No background tint.

Dark-mode overrides for: card background, shadow intensity, unread dot colour, monogram contrast.

## JavaScript (new in `static/reader.js`, ~40 lines)

Per-scroller:

```js
document.querySelectorAll('.feed-row__scroller').forEach((scroller) => {
  const track = scroller.querySelector('.feed-row__track');
  const prev = scroller.querySelector('.feed-row__arrow--prev');
  const next = scroller.querySelector('.feed-row__arrow--next');

  function cardStep() {
    const first = track.querySelector('.article-card');
    if (!first) return 260;
    return first.offsetWidth + 18; // gap 1.1rem ≈ 18px
  }

  function updateState() {
    const atStart = track.scrollLeft <= 1;
    const atEnd = track.scrollLeft + track.clientWidth >= track.scrollWidth - 1;
    scroller.dataset.atStart = atStart;
    scroller.dataset.atEnd = atEnd;
    prev.disabled = atStart;
    next.disabled = atEnd;
  }

  prev.addEventListener('click', () => track.scrollBy({left: -cardStep() * 2, behavior: 'smooth'}));
  next.addEventListener('click', () => track.scrollBy({left:  cardStep() * 2, behavior: 'smooth'}));
  track.addEventListener('scroll', updateState, {passive: true});
  window.addEventListener('resize', updateState);
  requestAnimationFrame(updateState);
});
```

No new dependencies. Same-origin, no CSP changes.

## Flask route changes (`feed_server.py`)

```python
@app.get("/")
def home():
    # Landing-page limits are fixed. max_item_per_feed (default 100) drives the
    # size of RSS XML feeds, which is a different concern.
    data = db.get_landing_data(latest_limit=10, per_sender_limit=10)
    # Sign every image_url + sender_favicon_url through img_proxy.sign_url.
    # Done inside db.get_landing_data to keep the route thin.
    return render_template("index.html", data=data)


@app.get("/list")
def xml_file_list():
    try:
        entries = sorted(
            p.name for p in FEED_DIR.iterdir()
            if p.is_file() and not p.name.endswith(".db")
        )
    except FileNotFoundError:
        entries = []
    return render_template("list.html", entries=entries)
```

Old `/`'s logic moves verbatim to `/list`.

## CSP impact

`/` renders same-origin assets (reader.css, reader.js, `/img` proxied images). Outer CSP (`default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-src 'self'`) covers all of these. No changes needed.

`reader.js` runs from `<script src="/static/reader.js">` — same-origin inline/external script rules apply. No `style-src 'unsafe-inline'`-requiring inline handlers added (handlers attached via `addEventListener`).

## Testing

Starting baseline: 127 tests.

### `tests/test_reader.py` (add ~5 tests)

- `test_extract_preview_image_picks_first_large_image`
- `test_extract_preview_image_skips_1x1_tracking_pixel` (by `width="1"` and by filename hint)
- `test_extract_preview_image_skips_cid_and_data`
- `test_extract_preview_image_returns_none_when_no_images`
- `test_extract_preview_image_normalizes_protocol_relative_to_https`

### `tests/test_util.py` (add ~6 tests)

- `test_relative_date_just_now`
- `test_relative_date_minutes`
- `test_relative_date_hours`
- `test_relative_date_yesterday`
- `test_relative_date_days_weeks_months_years`
- `test_relative_date_accepts_naive_and_aware`

### `tests/test_database.py` (add ~6 tests)

- `test_save_email_populates_preview_image_url`
- `test_save_email_stores_null_when_no_preview`
- `test_get_landing_data_returns_latest_and_rows`
- `test_get_landing_data_orders_latest_desc`
- `test_get_landing_data_limits_per_sender`
- `test_get_landing_data_empty_db_returns_empty_structure`

### `tests/test_feed_server.py` (add ~4 tests)

- `test_home_route_renders_feed_cards` — GET / → HTML contains `.feed-row` and `.article-card`
- `test_home_route_empty_state` — empty DB → "No feeds yet" message
- `test_list_route_renders_xml_filenames` — GET /list → `<a href="/<feed>.xml">`
- `test_home_route_includes_latest_section` — populate mixed emails → response contains "Latest" heading

### Migration test

- `test_migrate_adds_preview_image_url_column` — simulate pre-migration schema, run migrate, confirm column added
- `test_migrate_backfills_preview_images` — populate emails without the column, run migrate, confirm extracted URLs present

**Total impact**: 127 → ~150 tests.

## Acceptance criteria

1. All ~150 tests pass locally and in CI.
2. `poetry run ruff check .` clean, docker builds green.
3. Manual: hit `/`, see Latest section (portrait cards) + sender rows (landscape cards). Scrolling horizontally shows smooth snap, edge fade opacity changes with position, arrows appear on hover.
4. Manual: click a Latest card → opens `/article/<feed>/<guid>` (internal reader).
5. Manual: click a sender row title's "See all →" → opens `/article/<feed>` with filter=all.
6. Manual: view `/list` → old plain `<ul>` UI with XML links. Works regardless of `enable_internal_reader`.
7. Manual: view source on `/` — `<img>` tags for preview thumbnails point at `/img?u=...&sig=...` signed URLs.
8. Manual on mobile: swipe each row left/right → momentum scroll, snap-to-card. No arrow buttons visible.
9. Existing `/<feed>.xml` URLs behave identically (RSS readers unaffected).
10. On first run with an upgraded `emails.db`, migration adds `preview_image_url` column and backfills existing rows without data loss.

## Out of scope (explicitly deferred)

- Virtual scrolling / pagination for very large feeds (>100 rows). Limit stays at ~100 senders with ~10 articles each.
- Server-side image caching or thumbnail generation. Lazy `/img` proxy requests on first view are acceptable; browser cache handles repeat views.
- User-configurable row ordering (drag-to-reorder).
- Collapsible rows.
- Keyboard navigation between rows/cards.
- Accessibility polish beyond basic `aria-label` on arrow buttons and semantic `<section>`/`<h2>` structure. A WCAG pass is a separate concern.
- Right-to-left (RTL) layout.
- Custom favicon service (stays on Google s2).
- Offline/PWA caching of the landing page.
