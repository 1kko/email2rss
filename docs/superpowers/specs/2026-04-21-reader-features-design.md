# Reader Features: Read/Unread + Starred + FTS5 Search

**Status:** Approved design, ready for implementation plan
**Date:** 2026-04-21
**Scope:** Sub-project 4 of 4 in the broader reliability/feature improvement initiative

## Purpose

Turn the internal reader from a simple article viewer into a usable inbox-style interface. Add three features that materially change daily use:

1. **Read/unread state** with dwell-time auto-read (default 5 s) + manual "mark unread" button.
2. **Starred state** with manual toggle.
3. **Full-text search** across subject + plaintext body via SQLite FTS5.

## Non-goals

- Multi-user isolation. Still a single-user reader.
- Syncing read state to the RSS feed output. RSS clients track state locally; XML is unchanged.
- Keyboard shortcuts, bulk actions, or advanced search operators beyond FTS5's built-in syntax.
- Server-side snippet highlighting beyond FTS5's `snippet()` function.
- Undo for mark-as-read auto-fire. The manual "mark unread" button is the undo.

## Architecture

### Database schema

Two new columns on `Email`:

```python
is_read = Column(Boolean, default=False, nullable=False, server_default="0", index=True)
is_starred = Column(Boolean, default=False, nullable=False, server_default="0", index=True)
```

Both indexed because filter queries hit them heavily.

One new FTS5 virtual table (external-content, keyed to `Email.id`):

```sql
CREATE VIRTUAL TABLE emails_fts USING fts5(
    subject,
    body_text,
    content='emails',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
```

External-content means FTS5 does not duplicate the indexed text. `subject` lives on the main table; `body_text` is derived from the `content` BLOB at insert time (HTML stripped to plain text) and written into the FTS table directly. A delete trigger keeps FTS in sync on removals.

`tokenize='unicode61 remove_diacritics 2'` gives Unicode-aware tokenization with diacritic-insensitive matching (e.g. `cafe` matches `café`).

### Migration

`database.migrate_database()` is extended:

1. Add the `is_read` and `is_starred` columns via `ALTER TABLE emails ADD COLUMN ... DEFAULT 0 NOT NULL` if missing.
2. Create `emails_fts` virtual table + `emails_ad` delete trigger if missing.
3. If the FTS table exists but is empty AND the main table has rows, backfill in a single `INSERT INTO emails_fts(rowid, subject, body_text) SELECT ...` query (HTML-stripped via a Python helper called row-by-row in a loop, since SQL doesn't strip HTML natively).

Safe to run on both new and existing databases. Backfill runs once; subsequent startups skip because FTS is already populated.

### `save_email` extension

`database.save_email(...)` grows one line: after inserting the Email row, it also inserts into `emails_fts` with the pre-computed body_text. This replaces a trigger-based approach — triggers can't run HTML stripping, so Python handles it.

Helper in `database.py`:

```python
def _email_to_body_text(content_bytes: bytes) -> str:
    """Decode MIME, extract text/plain or strip tags from text/html, return plain text for indexing."""
```

Reuses `reader.extract_body_and_cid_map()` to get the HTML, then strips tags via `bleach.clean(html, tags=[], strip=True)` — which yields pure text with no markup.

### New database helpers

```python
def mark_read(email_id: int, is_read: bool) -> None
def mark_starred(email_id: int, is_starred: bool) -> None
def search_emails(query: str, limit: int = 50) -> list[dict]
def get_emails_filtered(sender: str | None, filter_mode: str, limit: int) -> list[dict]
def get_email_by_guid_with_state(sender: str, guid: str) -> Email | None  # returns Email with is_read/is_starred attrs
```

`search_emails` shape:

```python
[
    {
        "sender": str,
        "subject": str,
        "date": str,
        "guid": str,
        "snippet": str,    # FTS5 snippet() output, contains <b>...</b> around matches
        "feed_name": str,
    },
    ...
]
```

`get_emails_filtered` accepts `filter_mode` in `{"all", "unread", "starred"}`; any other value is rejected with `ValueError`. When `filter_mode == "unread"`, WHERE clause adds `is_read = 0`. When `"starred"`, adds `is_starred = 1`. When `sender` is None, queries across all senders.

`search_emails` invalid query handling: FTS5 raises `sqlite3.OperationalError` on malformed MATCH expressions. `search_emails` catches and re-raises as a new `database.SearchSyntaxError(Exception)` so callers can render a user-facing message without depending on sqlite internals.

### Backend routes

| Route | Method | Purpose |
|-------|--------|---------|
| `POST /article/<feed>/<guid>/read` | POST | Mark as read; returns `{"is_read": true}` |
| `DELETE /article/<feed>/<guid>/read` | DELETE | Mark as unread; returns `{"is_read": false}` |
| `POST /article/<feed>/<guid>/star` | POST | Star; returns `{"is_starred": true}` |
| `DELETE /article/<feed>/<guid>/star` | DELETE | Unstar; returns `{"is_starred": false}` |
| `GET /search?q=<query>` | GET | Search results page |
| `GET /article[?filter=all\|unread\|starred]` | GET | Article list (query param extended) |
| `GET /article/<feed>[?filter=...]` | GET | Per-sender list (query param extended) |

**REST shape:** POST marks, DELETE unmarks. No toggle semantics — the JS caller knows which state to set next and picks the method explicitly.

**Unknown guid** on any of the state-mutation routes returns 404.

**Empty search query** (`/search?q=` or missing `q`) renders the search page with no results (prompt to search), not a 400.

**Invalid FTS5 query syntax** (`search_emails` raises `SearchSyntaxError`) renders the search page with an error message; returns 200 still (it's a form-input failure, not a protocol failure).

**CSRF:** Same-origin check on mutating methods (POST/DELETE) via the `Origin` header:

```python
def _assert_same_origin():
    origin = request.headers.get("Origin")
    if origin and origin != config["server_baseurl"].rstrip("/"):
        abort(403)
```

If `Origin` header is absent (e.g., curl), allow. For a single-user tool behind localhost or Tailscale, this check is sufficient. No CSRF tokens.

**Config addition** in `common.py`:

```python
"read_after_seconds": int(os.getenv("read_after_seconds", "5")),
```

Passed into templates so JS can read it.

### `view_article` changes

No longer auto-marks read. Continues to render the iframe. New context vars passed to `article.html`:

- `is_read` (bool) — current state, used by template to decide which button state to render
- `is_starred` (bool)
- `feed_name`, `guid` — already present but now also used by JS data attributes
- `read_after_seconds` — from config

The record lookup already used `get_email_by_guid`; replace with `get_email_by_guid_with_state` which returns the Email including read/starred columns.

### Templates

**`templates/base.html`** — header nav with search form and link to all articles:

```html
<nav class="site-nav">
  <a href="/article">All Articles</a>
  <form action="/search" method="get">
    <input type="search" name="q" placeholder="Search..." value="{{ search_q or '' }}" required>
  </form>
</nav>
```

`search_q` defaults to empty in Jinja if not passed.

**`templates/article_list.html`** — filter chips at top, read/star indicators per row:

```html
<nav class="filter-chips">
  <a href="?filter=all" class="{% if filter_mode == 'all' %}active{% endif %}">All</a>
  <a href="?filter=unread" class="{% if filter_mode == 'unread' %}active{% endif %}">Unread</a>
  <a href="?filter=starred" class="{% if filter_mode == 'starred' %}active{% endif %}">Starred</a>
</nav>

<ul class="article-list">
  {% for article in articles %}
    <li class="{% if not article.is_read %}unread{% endif %}">
      {% if article.is_starred %}<span class="star">★</span>{% endif %}
      <a href="/article/{{ article.feed_name }}/{{ article.guid }}">{{ article.subject }}</a>
      <span class="meta">{{ article.sender }} · {{ article.date }}</span>
    </li>
  {% endfor %}
</ul>
```

Adjacent articles extend existing metadata dicts with `is_read` and `is_starred` — `get_emails_filtered` produces dicts already including these fields.

**`templates/article.html`** — toolbar:

```html
<article class="article"
         data-feed="{{ feed_name }}"
         data-guid="{{ guid }}"
         data-read-after-seconds="{{ read_after_seconds }}"
         data-is-read="{{ 'true' if is_read else 'false' }}"
         data-is-starred="{{ 'true' if is_starred else 'false' }}">
  <header>
    <h1>{{ subject }}</h1>
    <p class="meta">From: {{ sender }} | Date: {{ date }}</p>
    <div class="article-actions">
      <button id="star-btn" aria-label="Toggle star">
        <span class="star-icon">{{ '★' if is_starred else '☆' }}</span>
      </button>
      <button id="unread-btn" aria-label="Mark as unread">Mark unread</button>
    </div>
  </header>
  <iframe id="email-body" class="email-body-iframe"
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          srcdoc="{{ iframe_document|e }}"
          referrerpolicy="no-referrer" loading="lazy"
          title="Email body"></iframe>
</article>
```

Data attributes live on the `<article>` element rather than `<body>` so reader.js can feature-detect ("is this page an article page?") by querying `.article[data-feed]`.

**`templates/search_results.html`** (new):

```html
{% extends "base.html" %}
{% block title %}Search: {{ query }}{% endblock %}
{% block body %}
<h1>Search results for "{{ query }}"</h1>
{% if error %}
  <p class="error">Search error: {{ error }}</p>
{% elif not query %}
  <p>Enter a query in the search box above to search across emails.</p>
{% elif not results %}
  <p>No results.</p>
{% else %}
  <p>{{ results|length }} result{% if results|length != 1 %}s{% endif %}:</p>
  <ul class="search-results">
    {% for r in results %}
      <li>
        <a href="/article/{{ r.feed_name }}/{{ r.guid }}">{{ r.subject }}</a>
        <span class="meta">{{ r.sender }} · {{ r.date }}</span>
        <p class="snippet">{{ r.snippet|safe }}</p>
      </li>
    {% endfor %}
  </ul>
{% endif %}
{% endblock %}
```

`snippet` is rendered with `|safe` because FTS5's `snippet()` only inserts `<b>...</b>` wrappers — controlled server-side. The actual email text is HTML-escaped by FTS5 itself where needed.

### Frontend JS

**`static/reader.js`** grows from the one-comment file to ~40 lines:

```js
document.addEventListener('DOMContentLoaded', () => {
  const article = document.querySelector('.article[data-feed]');
  if (!article) return;

  const feed = article.dataset.feed;
  const guid = article.dataset.guid;
  const readAfter = Number(article.dataset.readAfterSeconds) || 5;
  const isReadInitially = article.dataset.isRead === 'true';

  // Dwell timer: only fire if not already read
  if (!isReadInitially) {
    setTimeout(() => {
      fetch(`/article/${feed}/${guid}/read`, { method: 'POST', credentials: 'same-origin' });
    }, readAfter * 1000);
  }

  // Star button
  const starBtn = document.getElementById('star-btn');
  starBtn?.addEventListener('click', async () => {
    const currentlyStarred = article.dataset.isStarred === 'true';
    const method = currentlyStarred ? 'DELETE' : 'POST';
    const resp = await fetch(`/article/${feed}/${guid}/star`, { method, credentials: 'same-origin' });
    if (resp.ok) {
      const data = await resp.json();
      article.dataset.isStarred = data.is_starred ? 'true' : 'false';
      starBtn.querySelector('.star-icon').textContent = data.is_starred ? '★' : '☆';
    }
  });

  // Unread button — after marking unread, redirect back to the list
  const unreadBtn = document.getElementById('unread-btn');
  unreadBtn?.addEventListener('click', async () => {
    await fetch(`/article/${feed}/${guid}/read`, { method: 'DELETE', credentials: 'same-origin' });
    window.location.href = '/article';
  });
});
```

### CSS additions (`static/reader.css`)

~30 lines covering:

- `.site-nav` — flex row, pushes search to the right
- `.site-nav input[type="search"]` — sensible width, padding, border
- `.filter-chips` — inline flex row of small rounded chips; `.active` gets a background
- `.article-list li.unread` — bold subject
- `.article-list .star` — gold color (`#d4a017`)
- `.article-actions` — flex row inside article header; buttons get a subtle border
- `.search-results .snippet` — muted text color, smaller font
- `.search-results .snippet b` — yellow background highlight for matches

No framework. Pragmatic plain CSS in keeping with the existing reader.css style.

## Testing

Starting count: 92.

### `tests/test_database.py` additions (~12 tests)

- `test_mark_read_flips_flag`
- `test_mark_read_unflip`
- `test_mark_starred_flips_flag`
- `test_mark_starred_unflip`
- `test_get_emails_filtered_unread_only`
- `test_get_emails_filtered_starred_only`
- `test_get_emails_filtered_all_returns_all`
- `test_get_emails_filtered_rejects_invalid_mode`
- `test_search_emails_finds_match_in_subject`
- `test_search_emails_finds_match_in_body`
- `test_search_emails_returns_snippet_with_bold_markup`
- `test_search_emails_invalid_syntax_raises_SearchSyntaxError`
- `test_fts_index_is_updated_on_insert` — insert, search immediately finds it
- `test_fts_index_is_cleaned_on_delete` — delete via `delete_emails_older_than`, search no longer finds it

### `tests/test_feed_server.py` additions (~10 tests)

- `test_mark_read_route_sets_flag`
- `test_unmark_read_route_clears_flag`
- `test_star_route_sets_flag`
- `test_unstar_route_clears_flag`
- `test_mark_read_route_404s_for_unknown_guid`
- `test_star_route_rejects_cross_origin` — set `Origin: https://evil.example` → 403
- `test_article_list_filter_unread` — populate mixed, GET `?filter=unread` → HTML contains unread, not read
- `test_article_list_filter_starred`
- `test_search_route_returns_results` — insert email, GET `/search?q=<term>` → 200, HTML contains subject
- `test_search_route_empty_query_renders_prompt` — GET `/search` without q → 200 with "Enter a query" prompt
- `test_search_route_invalid_query_renders_error` — GET `/search?q=AND` → 200 with error message
- `test_article_page_does_not_auto_mark_read_server_side` — GET `/article/f/g` → is_read unchanged in DB (auto-read is client-side)
- `test_article_page_passes_read_after_seconds_to_template` — response HTML contains `data-read-after-seconds="5"`

### Migration tests

- `test_migrate_adds_is_read_column_if_missing` — create schema without is_read, run migrate, assert column present
- `test_migrate_creates_fts_table_if_missing`
- `test_migrate_backfills_fts_on_existing_rows` — populate rows before FTS exists, run migrate, search finds them

### Final count

Target: ~112 tests.

## Config

New env vars:

- `read_after_seconds` (int, default 5) — client-side dwell timer

No other config changes.

## Dependencies

- `bleach` — already present (Task 3 of sub-project 2); reused for HTML stripping in `_email_to_body_text`.
- No new deps.

## Acceptance criteria

1. All ~112 tests pass locally and in CI.
2. Manual: open `/article`, see filter chips and unread-bold styling.
3. Manual: open an article, wait 5s — reload `/article`, that article is no longer bold (read).
4. Manual: click star on an article, reload `/article?filter=starred`, starred article appears.
5. Manual: click "Mark unread" on a read article → redirect to `/article`, that article is bold again.
6. Manual: search for a word that appears in a newsletter body (e.g. "unsubscribe") → results page shows multiple hits with highlighted snippets.
7. Manual: search with invalid syntax (`/search?q=AND`) → friendly error message, not a 500.
8. Manual: `curl -X POST http://localhost:8000/article/x/y/read -H "Origin: https://evil.example"` returns 403.
9. On first run with an existing (pre-sub-project-4) `emails.db`, migration adds columns + FTS table + backfills without data loss.

## Out of scope (explicitly deferred)

- Bulk operations (mark all as read, mark all in sender as read)
- Keyboard shortcuts
- Search operators beyond FTS5 built-ins (date filter, sender filter)
- Search result pagination (hard-capped at 50 results)
- Advanced CSRF protection (tokens, double-submit cookies)
- Persisting search queries / history
- Tagging or folders beyond starred
- Dark-mode styling tweaks for new UI elements (existing `@media (prefers-color-scheme: dark)` in iframe CSS is untouched; outer reader.css dark mode is a separate polish pass)
- Syncing read/starred state into the RSS XML (RSS clients track locally)
