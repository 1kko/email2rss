# Landing Page Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `/`'s plain XML file listing with a Netflix-style landing that shows a Latest section (10 newest articles across all feeds, portrait cards) and per-sender rows (landscape cards, horizontal scroll). Preserve the old listing at `/list`. `/<feed>.xml` and all article-reader URLs unchanged.

**Architecture:** New `preview_image_url` column on `Email` stores the article's hero image URL, extracted at `save_email` time from the email HTML via a new `reader.extract_preview_image(msg)` helper (skips 1×1 trackers, skips `cid:`/`data:`, requires `http(s)` or protocol-relative, requires ≥ 50px when dimensions are declared). A new `database.get_landing_data(latest_limit, per_sender_limit)` function returns the page's shape in one call — `latest` list + `rows` list — with all image URLs signed through the existing `img_proxy.sign_url`. Frontend: `templates/index.html` rewrites into Jinja macros (`_cards.html`) rendering cards; `static/reader.css` gains ~180 lines for scroller/cards; `static/reader.js` gains ~40 lines to drive hover arrow buttons + edge-fade opacity based on scroll position.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Flask, Jinja2, bleach (already present), SQLite, pytest.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `database.py` | modify | New `preview_image_url` column on `Email`; `migrate_database()` adds column + backfills existing rows; `save_email()` populates it; new `get_landing_data()`; new `_article_dict()` helper |
| `reader.py` | modify | New `extract_preview_image(msg)` function |
| `util.py` | modify | New `relative_date(dt)` + `monogram_hue(sender)` helpers |
| `feed_server.py` | modify | Rewrite `/` route to use `get_landing_data`; new `/list` route for the old plain listing |
| `templates/index.html` | modify (full rewrite) | Netflix-style landing |
| `templates/_cards.html` | **create** | Jinja macros: `scroller`, `portrait_card`, `landscape_card`, `row_favicon` |
| `templates/list.html` | **create** | Old `/` plain-list content, moved verbatim |
| `static/reader.css` | modify | Appends ~180 lines for `.feed-row*`, `.article-card*`, `.latest-section`, arrow buttons, edge fades, dark-mode overrides |
| `static/reader.js` | modify | Appends ~40 lines wiring arrow buttons + scroll-state attributes for each `.feed-row__scroller` |
| `tests/test_reader.py` | modify | ~5 tests for `extract_preview_image` |
| `tests/test_util.py` | modify | ~7 tests for `relative_date` + `monogram_hue` |
| `tests/test_database.py` | modify | ~8 tests for `preview_image_url` migration/population + `get_landing_data` |
| `tests/test_feed_server.py` | modify | ~4 tests for `/` and `/list` routes |
| `README.md` | modify | Brief note about new landing UI; `/list` for XML list |

No new runtime dependencies.

---

## Task 1: `extract_preview_image` + `preview_image_url` column + save_email extension

**Files:**
- Modify: `reader.py`
- Modify: `database.py`
- Modify: `tests/test_reader.py`
- Modify: `tests/test_database.py`
- Modify: `tests/conftest.py` (if the db_session fixture needs any tweak — probably not)

- [ ] **Step 1.1: Write failing tests for `extract_preview_image`**

Append to `tests/test_reader.py`:

```python
def _make_html_msg(html_body: str, sender="s@example.com") -> "email.message.Message":
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
```

- [ ] **Step 1.2: Run tests (they fail)**

```bash
poetry run pytest tests/test_reader.py -v -k extract_preview_image
```

Expected: `AttributeError: module 'reader' has no attribute 'extract_preview_image'`.

- [ ] **Step 1.3: Implement `extract_preview_image` in `reader.py`**

Append to `reader.py`:

```python
_TRACKING_FILENAME_HINTS = ("pixel", "track", "open", "beacon", "spacer")
_MIN_IMAGE_PX = 50


def extract_preview_image(msg) -> str | None:
    """
    Walk the MIME message's HTML body and return the first "usable" <img> URL,
    or None. Used for landing-page thumbnails.

    Rules for "usable":
    - src starts with http://, https://, or // (protocol-relative → https:)
    - cid: and data: skipped (can't be re-served via the /img proxy from a list)
    - width=1 or height=1 skipped (tracking pixel)
    - filename containing pixel/track/open/beacon/spacer skipped (case-insensitive)
    - if width and height attributes are both declared, both must be >= 50
    - otherwise accepted
    """
    import re

    body_html, _cid_map = extract_body_and_cid_map(msg)
    if not body_html:
        return None

    # Find all <img ...> tags. We keep the regex narrow: we only inspect src/width/height.
    # A full HTML parse is overkill for this single-field extraction.
    for match in re.finditer(r'<img\b([^>]*)>', body_html, flags=re.IGNORECASE):
        attrs_str = match.group(1)
        src = _attr(attrs_str, "src")
        if not src:
            continue
        src = src.strip()
        lower_src = src.lower()

        # Skip unsupported schemes
        if lower_src.startswith("cid:") or lower_src.startswith("data:"):
            continue

        # Normalize protocol-relative
        if src.startswith("//"):
            src = "https:" + src
            lower_src = src.lower()

        if not (lower_src.startswith("http://") or lower_src.startswith("https://")):
            continue

        # Filename hint filter
        fname = lower_src.rsplit("/", 1)[-1]
        if any(hint in fname for hint in _TRACKING_FILENAME_HINTS):
            continue

        width = _attr_int(attrs_str, "width")
        height = _attr_int(attrs_str, "height")

        # 1x1 tracking pixel
        if width == 1 or height == 1:
            continue

        # If both declared and either is below threshold, skip
        if width is not None and height is not None:
            if width < _MIN_IMAGE_PX or height < _MIN_IMAGE_PX:
                continue

        return src

    return None


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
```

- [ ] **Step 1.4: Run tests (they pass)**

```bash
poetry run pytest tests/test_reader.py -v -k extract_preview_image
```

Expected: 8 tests PASS.

- [ ] **Step 1.5: Add `preview_image_url` column to Email model + migration + tests**

Edit `database.py` Email class (around line 25, after `is_starred`):

```python
    is_read = Column(Boolean, default=False, nullable=False, server_default="0", index=True)
    is_starred = Column(Boolean, default=False, nullable=False, server_default="0", index=True)
    preview_image_url = Column(String, nullable=True)  # extracted hero image; None if no usable image
```

Extend `migrate_database()` where column existence is checked (search for `if "is_starred" not in existing_cols:` and add after that block):

```python
        if "preview_image_url" not in existing_cols:
            logging.info("Adding column: preview_image_url")
            conn.execute(text(
                "ALTER TABLE emails ADD COLUMN preview_image_url TEXT"
            ))
```

Extend the backfill block at the end of `migrate_database()` (the section that backfills FTS for existing rows — add a second backfill step immediately after):

```python
        # Backfill preview_image_url for rows added before the column existed
        missing_preview = conn.execute(
            text("SELECT COUNT(*) FROM emails WHERE preview_image_url IS NULL")
        ).scalar()
        if missing_preview and main_count > 0:
            logging.info(f"Backfilling preview_image_url for {missing_preview} rows...")
            _backfill_preview_images(conn)
```

Add the helper alongside `_backfill_fts_index`:

```python
def _backfill_preview_images(conn):
    """Populate preview_image_url for emails that predate the column. One-time."""
    import reader  # local import to avoid circular dep

    rows = conn.execute(
        text("SELECT id, content FROM emails WHERE preview_image_url IS NULL")
    ).fetchall()
    for row_id, content in rows:
        try:
            msg = email.message_from_bytes(content)
            preview = reader.extract_preview_image(msg)
        except Exception:
            preview = None
            logging.warning(f"preview backfill: extraction failed for id={row_id}")
        conn.execute(
            text("UPDATE emails SET preview_image_url = :p WHERE id = :id"),
            {"p": preview, "id": row_id},
        )
    conn.commit()
    logging.info(f"preview_image_url backfill complete: {len(rows)} rows processed")
```

- [ ] **Step 1.6: Extend `save_email` to populate `preview_image_url`**

In `database.py`, modify the existing `save_email` function body — where the FTS insert already happens, add the preview extraction alongside it. Replace the block that already calls `reader.extract_plain_text` with:

```python
            # After commit we know new_email.id — write matching FTS row + preview URL
            try:
                msg = email.message_from_bytes(content)
                body_text = reader.extract_plain_text(msg)
                preview = reader.extract_preview_image(msg)
            except Exception:
                body_text = ""
                preview = None
                logging.warning(f"save_email: extraction failed for email_id={email_id}")
            session.execute(
                text("INSERT INTO emails_fts(rowid, subject, body_text) VALUES (:id, :s, :b)"),
                {"id": new_email.id, "s": _html_escape.escape(subject or ""), "b": body_text},
            )
            if preview is not None:
                session.execute(
                    text("UPDATE emails SET preview_image_url = :p WHERE id = :id"),
                    {"p": preview, "id": new_email.id},
                )
            session.commit()
```

- [ ] **Step 1.7: Write failing tests for column + save_email + backfill**

Append to `tests/test_database.py`:

```python
def test_email_model_has_preview_image_url_column(db_session):
    """Fresh in-memory DB should have the new column, default None."""
    insert_email(db_session, email_id=1)
    row = db_session.query(db.Email).filter_by(email_id=1).first()
    assert row.preview_image_url is None


def test_save_email_populates_preview_image_url(db_session):
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<p>body</p><img src="http://cdn.example.com/hero.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=2,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    row = db_session.query(db.Email).filter_by(email_id=2).first()
    assert row.preview_image_url == "http://cdn.example.com/hero.jpg"


def test_save_email_leaves_preview_null_when_no_usable_image(db_session):
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>no images</p>"
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=3,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    row = db_session.query(db.Email).filter_by(email_id=3).first()
    assert row.preview_image_url is None


def test_backfill_preview_images_populates_existing_rows(db_session):
    """Simulate a pre-migration DB: insert a row with an image, then clear
    preview_image_url, then run _backfill_preview_images."""
    from sqlalchemy import text as _text

    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<img src="http://x.com/pic.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=4,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    # Reset to simulate pre-column state
    with db.engine.connect() as conn:
        conn.execute(_text("UPDATE emails SET preview_image_url = NULL"))
        conn.commit()
        db._backfill_preview_images(conn)

    row = db_session.query(db.Email).filter_by(email_id=4).first()
    assert row.preview_image_url == "http://x.com/pic.jpg"
```

- [ ] **Step 1.8: Run tests (they pass)**

```bash
poetry run pytest tests/test_database.py -v -k "preview_image_url or backfill_preview"
```

Expected: 4 tests pass.

Full suite:
```bash
poetry run pytest -v
```

Expected: ~127 + 8 reader + 4 db = ~139 tests.

- [ ] **Step 1.9: Commit**

```bash
git add reader.py database.py tests/test_reader.py tests/test_database.py
git commit -m "feat: extract preview image per email, cache in preview_image_url column"
```

---

## Task 2: `util.relative_date` + `util.monogram_hue`

**Files:**
- Modify: `util.py`
- Modify: `tests/test_util.py`

- [ ] **Step 2.1: Write failing tests**

Append to `tests/test_util.py`:

```python
import datetime
from util import relative_date, monogram_hue


def _now():
    return datetime.datetime(2026, 4, 21, 15, 0, 0)


def test_relative_date_just_now():
    dt = _now() - datetime.timedelta(seconds=30)
    assert relative_date(dt, now=_now()) == "방금 전"


def test_relative_date_minutes():
    dt = _now() - datetime.timedelta(minutes=5)
    assert relative_date(dt, now=_now()) == "5분 전"


def test_relative_date_hours():
    dt = _now() - datetime.timedelta(hours=3)
    assert relative_date(dt, now=_now()) == "3시간 전"


def test_relative_date_yesterday():
    dt = _now() - datetime.timedelta(hours=20)  # previous calendar day
    assert relative_date(dt, now=_now()) == "어제"


def test_relative_date_days():
    dt = _now() - datetime.timedelta(days=3)
    assert relative_date(dt, now=_now()) == "3일 전"


def test_relative_date_weeks():
    dt = _now() - datetime.timedelta(days=10)
    assert relative_date(dt, now=_now()) == "1주 전"


def test_relative_date_months():
    dt = _now() - datetime.timedelta(days=60)
    assert relative_date(dt, now=_now()) == "2개월 전"


def test_relative_date_years():
    dt = _now() - datetime.timedelta(days=400)
    assert relative_date(dt, now=_now()) == "1년 전"


def test_relative_date_accepts_aware_datetime():
    """tz-aware input shouldn't crash — compare in UTC."""
    now_aware = datetime.datetime(2026, 4, 21, 15, 0, 0, tzinfo=datetime.timezone.utc)
    dt = now_aware - datetime.timedelta(hours=2)
    assert relative_date(dt, now=now_aware) == "2시간 전"


def test_monogram_hue_is_deterministic():
    assert monogram_hue("alice@example.com") == monogram_hue("alice@example.com")
    assert 0 <= monogram_hue("alice@example.com") < 360


def test_monogram_hue_differs_for_different_senders():
    # Almost any hash fn will distinguish these two
    assert monogram_hue("alice@example.com") != monogram_hue("bob@example.com")
```

- [ ] **Step 2.2: Run tests (they fail)**

```bash
poetry run pytest tests/test_util.py -v -k "relative_date or monogram_hue"
```

Expected: `ImportError: cannot import name 'relative_date' from 'util'`.

- [ ] **Step 2.3: Implement the helpers**

Append to `util.py`:

```python
import datetime
import hashlib


def relative_date(dt: datetime.datetime, now: datetime.datetime | None = None) -> str:
    """
    Return a Korean-localized relative time string for `dt`.

    Accepts naive or tz-aware datetimes. If one side is naive and the other
    aware, the aware side is coerced to naive UTC for the comparison.
    `now` is injectable for deterministic tests.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc) if dt.tzinfo else datetime.datetime.now()

    # Normalize both sides to the same naive/aware shape
    if dt.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif dt.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    delta = now - dt
    total_seconds = delta.total_seconds()
    if total_seconds < 60:
        return "방금 전"
    if total_seconds < 3600:
        return f"{int(total_seconds // 60)}분 전"

    # "어제" only if `dt` falls on the previous calendar day AND < 48h gap
    if total_seconds < 48 * 3600:
        now_date = now.date()
        dt_date = dt.date() if dt.tzinfo is None else dt.astimezone(datetime.timezone.utc).date()
        if dt_date == now_date:
            return f"{int(total_seconds // 3600)}시간 전"
        if dt_date == now_date - datetime.timedelta(days=1):
            return "어제"

    days = delta.days
    if days < 7:
        return f"{days}일 전"
    if days < 30:
        return f"{days // 7}주 전"
    if days < 365:
        return f"{days // 30}개월 전"
    return f"{days // 365}년 전"


def monogram_hue(sender: str) -> int:
    """
    Return a deterministic HSL hue (0-359) for a sender string.
    Used for monogram fallback background color on landing cards.
    """
    h = hashlib.md5(sender.encode("utf-8"), usedforsecurity=False).digest()
    return h[0] % 360
```

- [ ] **Step 2.4: Run tests (they pass)**

```bash
poetry run pytest tests/test_util.py -v -k "relative_date or monogram_hue"
```

Expected: 11 tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add util.py tests/test_util.py
git commit -m "feat: add util.relative_date (Korean) and util.monogram_hue"
```

---

## Task 3: `database.get_landing_data`

**Files:**
- Modify: `database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 3.1: Write failing tests**

Append to `tests/test_database.py`:

```python
def test_get_landing_data_empty_db(db_session):
    data = db.get_landing_data(latest_limit=10, per_sender_limit=10)
    assert data == {"latest": [], "rows": []}


def test_get_landing_data_returns_latest_and_rows(db_session):
    # alice: 2 articles. bob: 1 article.
    insert_email(db_session, email_id=1, sender="alice@example.com",
                 timestamp=datetime.datetime(2026, 4, 10))
    insert_email(db_session, email_id=2, sender="alice@example.com",
                 timestamp=datetime.datetime(2026, 4, 15))
    insert_email(db_session, email_id=3, sender="bob@example.com",
                 timestamp=datetime.datetime(2026, 4, 12))

    data = db.get_landing_data(latest_limit=10, per_sender_limit=10)
    assert len(data["latest"]) == 3
    # Latest ordered by timestamp desc
    assert data["latest"][0]["sender"] == "alice@example.com"  # 2026-04-15
    assert data["latest"][1]["sender"] == "bob@example.com"    # 2026-04-12
    assert data["latest"][2]["sender"] == "alice@example.com"  # 2026-04-10

    # Rows ordered by each sender's newest article desc — alice (04-15) before bob (04-12)
    assert [r["sender"] for r in data["rows"]] == ["alice@example.com", "bob@example.com"]
    alice_row = data["rows"][0]
    bob_row = data["rows"][1]
    assert alice_row["article_count"] == 2
    assert bob_row["article_count"] == 1
    # Per-row articles sorted newest-first
    assert len(alice_row["articles"]) == 2
    assert alice_row["articles"][0]["sender"] == "alice@example.com"


def test_get_landing_data_limits_latest(db_session):
    for i in range(15):
        insert_email(db_session, email_id=i,
                     timestamp=datetime.datetime(2026, 4, 10) + datetime.timedelta(hours=i))
    data = db.get_landing_data(latest_limit=5, per_sender_limit=10)
    assert len(data["latest"]) == 5


def test_get_landing_data_limits_per_sender(db_session):
    for i in range(15):
        insert_email(db_session, email_id=i, sender="alice@example.com",
                     timestamp=datetime.datetime(2026, 4, 10) + datetime.timedelta(hours=i))
    data = db.get_landing_data(latest_limit=10, per_sender_limit=5)
    assert data["rows"][0]["article_count"] == 15
    assert len(data["rows"][0]["articles"]) == 5


def test_get_landing_data_includes_favicon_and_monogram(db_session):
    insert_email(db_session, email_id=1, sender="alice@example.com")
    data = db.get_landing_data()
    row = data["rows"][0]
    assert row["favicon_url"]  # non-empty signed URL
    assert row["monogram_letter"] == "A"
    assert 0 <= row["monogram_hue"] < 360


def test_get_landing_data_signs_preview_urls(db_session):
    """preview_image_url values flow through img_proxy.sign_url — each card's
    image_url should start with the /img? prefix, not be the bare remote URL."""
    content = (
        b"From: s@example.com\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<img src="http://cdn.example.com/hero.jpg" width="600" height="400">'
    )
    db.save_email(
        sender="s@example.com", receiver="me@localhost", email_id=1,
        subject="t", content=content, timestamp=datetime.datetime(2026, 4, 13),
    )
    data = db.get_landing_data()
    article = data["latest"][0]
    assert article["image_url"]
    assert "/img?u=" in article["image_url"]
    assert "&sig=" in article["image_url"]
```

- [ ] **Step 3.2: Run tests (they fail)**

```bash
poetry run pytest tests/test_database.py -v -k get_landing_data
```

Expected: `AttributeError: module 'database' has no attribute 'get_landing_data'`.

- [ ] **Step 3.3: Implement `get_landing_data` and `_article_dict`**

Append to `database.py`:

```python
def _sender_domain(sender: str) -> str:
    """Extract domain from a sender email address; '' if no @."""
    if "@" in sender:
        return sender.split("@", 1)[1].lower()
    return ""


def _sanitize_feed_name(sender: str) -> str:
    """Match feed_generator.py's convention: replace @ and . with _."""
    return sender.replace("@", "_").replace(".", "_")


def _article_dict(row: Email, sign_url) -> dict:
    """Shape a single article for landing-page rendering."""
    import util

    try:
        msg = email.message_from_bytes(row.content)
        subject = str(email.header.make_header(email.header.decode_header(msg["subject"])))
        unique_string = (msg["subject"] or "") + (msg["date"] or "") + (msg["from"] or "")
        guid = hashlib.md5(unique_string.encode(), usedforsecurity=False).hexdigest()
        date_str = msg["date"] or ""
    except Exception:
        subject = row.subject or ""
        guid = ""
        date_str = ""

    domain = _sender_domain(row.sender)
    favicon_raw = (
        f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        if domain else None
    )
    local_part = row.sender.split("@", 1)[0] if "@" in row.sender else row.sender
    letter = local_part[0].upper() if local_part else "?"

    return {
        "sender": row.sender,
        "subject": subject,
        "date_str": date_str,
        "relative_date": util.relative_date(row.timestamp) if row.timestamp else "",
        "guid": guid,
        "feed_name": _sanitize_feed_name(row.sender),
        "image_url": sign_url(row.preview_image_url) if row.preview_image_url else None,
        "is_read": bool(row.is_read),
        "is_starred": bool(row.is_starred),
        "sender_favicon_url": sign_url(favicon_raw) if favicon_raw else None,
        "monogram_letter": letter,
        "monogram_hue": util.monogram_hue(row.sender),
    }


def get_landing_data(latest_limit: int = 10, per_sender_limit: int = 10) -> dict:
    """Return the landing-page payload. See spec for shape details."""
    from common import get_img_proxy_secret, config
    import img_proxy

    secret = get_img_proxy_secret()
    base = (config.get("server_baseurl") or "").rstrip("/")

    def sign(url):
        if not url:
            return None
        return img_proxy.sign_url(url, secret, base)

    with Session() as session:
        # Latest across all senders
        latest_rows = (
            session.query(Email)
            .order_by(Email.timestamp.desc())
            .limit(latest_limit)
            .all()
        )
        latest = [_article_dict(r, sign) for r in latest_rows]

        # Per-sender: get each sender's most recent timestamp (for row ordering)
        from sqlalchemy import func
        sender_tops = (
            session.query(Email.sender, func.max(Email.timestamp).label("t_max"),
                          func.count(Email.id).label("article_count"))
            .group_by(Email.sender)
            .order_by(func.max(Email.timestamp).desc())
            .all()
        )

        rows = []
        for sender, _t_max, article_count in sender_tops:
            sender_articles = (
                session.query(Email)
                .filter_by(sender=sender)
                .order_by(Email.timestamp.desc())
                .limit(per_sender_limit)
                .all()
            )
            if not sender_articles:
                continue
            dicts = [_article_dict(r, sign) for r in sender_articles]
            first = dicts[0]
            rows.append({
                "sender": sender,
                "feed_name": first["feed_name"],
                "favicon_url": first["sender_favicon_url"],
                "monogram_letter": first["monogram_letter"],
                "monogram_hue": first["monogram_hue"],
                "article_count": int(article_count),
                "articles": dicts,
            })

        return {"latest": latest, "rows": rows}
```

- [ ] **Step 3.4: Run tests (they pass)**

```bash
poetry run pytest tests/test_database.py -v -k get_landing_data
```

Expected: 6 tests pass.

Full suite:
```bash
poetry run pytest -v
```

Expected: ~139 + 6 = ~145 tests.

- [ ] **Step 3.5: Commit**

```bash
git add database.py tests/test_database.py
git commit -m "feat: add database.get_landing_data for Netflix-style landing"
```

---

## Task 4: `/` and `/list` route changes

**Files:**
- Modify: `feed_server.py`
- Modify: `tests/test_feed_server.py`

- [ ] **Step 4.1: Write failing tests**

Append to `tests/test_feed_server.py`:

```python
def test_home_route_renders_feed_grid(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")
    insert_email(db_session, email_id=1, sender="alice@example.com")

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "feed-row" in body
    assert "article-card" in body
    assert "alice@example.com" in body


def test_home_route_empty_state(client, db_session):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "No feeds yet" in body


def test_home_route_includes_latest_section(client, db_session, monkeypatch):
    from tests.conftest import insert_email
    monkeypatch.setitem(feed_server.config, "server_baseurl", "http://testserver")
    insert_email(db_session, email_id=1)

    resp = client.get("/")
    body = resp.data.decode("utf-8")
    assert "latest-section" in body or "Latest" in body


def test_list_route_renders_xml_filenames(client, tmp_path, monkeypatch):
    """/list lists the XML files in FEED_DIR (same content the old / used to show)."""
    (tmp_path / "hello_example_com.xml").write_text("<rss/>", encoding="utf-8")
    (tmp_path / "team_example_com.xml").write_text("<rss/>", encoding="utf-8")

    resp = client.get("/list")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert 'href="/hello_example_com.xml"' in body
    assert 'href="/team_example_com.xml"' in body


def test_list_route_empty_feed_dir(client, tmp_path):
    resp = client.get("/list")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # No file links, but page still renders
    assert "<ul>" in body or "No feeds" in body or "RSS Feeds" in body
```

- [ ] **Step 4.2: Run tests (they fail)**

```bash
poetry run pytest tests/test_feed_server.py -v -k "home_route or list_route"
```

Expected: the current `/` tests probably still pass (old route), but new assertions fail because the new template doesn't exist yet. `/list` tests fail with 404.

- [ ] **Step 4.3: Rewire `/` and add `/list` route**

In `feed_server.py`, find the current `/` route. Replace its body with:

```python
    @app.get("/")
    def home():
        # Landing-page limits are fixed. max_item_per_feed (default 100) drives
        # RSS XML feed size, a separate concern.
        data = db.get_landing_data(latest_limit=10, per_sender_limit=10)
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

(The existing `/` route had logic that listed `FEED_DIR.iterdir()` and rendered `index.html` with `entries=...`. That logic moves verbatim to `/list` above. `/` now uses the new landing template.)

- [ ] **Step 4.4: Run tests — they still fail because templates don't exist yet**

Tests will fail with `jinja2.exceptions.TemplateNotFound: list.html` (and index.html will fail differently because we haven't rewritten it yet).

That's expected — templates come in Task 5. Move on.

- [ ] **Step 4.5: Commit the route changes (as a staging commit)**

```bash
git add feed_server.py tests/test_feed_server.py
git commit -m "feat: rewire / to landing + new /list route (templates pending)"
```

---

## Task 5: Templates (index.html, _cards.html, list.html)

**Files:**
- Modify: `templates/index.html` (full rewrite)
- Create: `templates/_cards.html`
- Create: `templates/list.html`

- [ ] **Step 5.1: Create `templates/list.html`**

Full file:

```jinja2
{% extends "base.html" %}
{% block title %}email2rss — XML feed list{% endblock %}
{% block body %}
<h1>RSS Feeds (XML)</h1>
<p>Direct XML URLs for RSS readers. For the styled landing, <a href="/">go back to /</a>.</p>
{% if entries %}
<ul>
  {% for name in entries %}
    <li><a href="/{{ name }}">{{ name }}</a></li>
  {% endfor %}
</ul>
{% else %}
<p>No feeds yet. The IMAP fetcher will populate feeds once emails are fetched and RSS files are generated.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5.2: Create `templates/_cards.html` (Jinja macros)**

Full file:

```jinja2
{# Shared card / scroller macros for the landing page. #}

{% macro row_favicon(row) %}
  {% if row.favicon_url %}
    <img class="feed-row__title-favicon" src="{{ row.favicon_url }}" alt="" loading="lazy" referrerpolicy="no-referrer">
  {% else %}
    <span class="feed-row__title-monogram"
          style="background: hsl({{ row.monogram_hue }}, 55%, 50%)">
      {{ row.monogram_letter }}
    </span>
  {% endif %}
{% endmacro %}

{% macro _thumb(article) %}
  {% if article.image_url %}
    <img src="{{ article.image_url }}" alt="" loading="lazy" referrerpolicy="no-referrer">
  {% elif article.sender_favicon_url %}
    <img class="article-card__favicon" src="{{ article.sender_favicon_url }}" alt="" loading="lazy" referrerpolicy="no-referrer">
  {% else %}
    <span class="article-card__monogram"
          style="background: hsl({{ article.monogram_hue }}, 55%, 50%)">
      {{ article.monogram_letter }}
    </span>
  {% endif %}
{% endmacro %}

{% macro portrait_card(article) %}
  <a class="article-card article-card--portrait" href="/article/{{ article.feed_name }}/{{ article.guid }}">
    <div class="article-card__thumb">{{ _thumb(article) }}</div>
    <div class="article-card__body">
      <p class="article-card__sender-label">
        {% if article.sender_favicon_url %}
          <img src="{{ article.sender_favicon_url }}" alt="" loading="lazy" referrerpolicy="no-referrer">
        {% else %}
          <span style="display:inline-flex;width:14px;height:14px;border-radius:3px;background:hsl({{ article.monogram_hue }},55%,50%);color:#fff;align-items:center;justify-content:center;font-size:0.55rem;font-weight:700">{{ article.monogram_letter }}</span>
        {% endif %}
        {{ article.sender.split("@")[1] if "@" in article.sender else article.sender }}
      </p>
      <h3 class="article-card__subject">{{ article.subject }}</h3>
      <p class="article-card__date">{{ article.relative_date }}</p>
      {% if not article.is_read %}<span class="article-card__unread-dot" aria-label="unread"></span>{% endif %}
    </div>
  </a>
{% endmacro %}

{% macro landscape_card(article) %}
  <a class="article-card" href="/article/{{ article.feed_name }}/{{ article.guid }}">
    <div class="article-card__thumb">{{ _thumb(article) }}</div>
    <div class="article-card__body">
      <h3 class="article-card__subject">{{ article.subject }}</h3>
      <p class="article-card__date">{{ article.relative_date }}</p>
      {% if not article.is_read %}<span class="article-card__unread-dot" aria-label="unread"></span>{% endif %}
    </div>
  </a>
{% endmacro %}

{% macro scroller(articles, portrait=False) %}
  <div class="feed-row__scroller">
    <button class="feed-row__arrow feed-row__arrow--prev" type="button" aria-label="Previous">‹</button>
    <button class="feed-row__arrow feed-row__arrow--next" type="button" aria-label="Next">›</button>
    <div class="feed-row__track">
      {% for article in articles %}
        {% if portrait %}
          {{ portrait_card(article) }}
        {% else %}
          {{ landscape_card(article) }}
        {% endif %}
      {% endfor %}
    </div>
  </div>
{% endmacro %}
```

- [ ] **Step 5.3: Rewrite `templates/index.html`**

Replace the entire file content with:

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
        <h2 class="feed-row__title">✦ Latest</h2>
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

<script src="{{ url_for('static', filename='reader.js') }}"></script>
{% endblock %}
```

The `<script>` is included here so the scroller JS (added in Task 7) initializes on this page too.

- [ ] **Step 5.4: Run tests**

```bash
poetry run pytest -v
```

Expected: full suite passes (~145 + 5 feed_server = ~150). The route tests from Task 4 should now pass since templates exist.

- [ ] **Step 5.5: Commit**

```bash
git add templates/index.html templates/_cards.html templates/list.html
git commit -m "feat: Netflix-style landing templates (index, _cards macros, list)"
```

---

## Task 6: CSS

**Files:**
- Modify: `static/reader.css`

- [ ] **Step 6.1: Append the new CSS rules**

Append to `static/reader.css`:

```css
/* =============== Landing page (Netflix-style) =============== */

.feed-rows {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 1rem 3rem;
}

.feed-rows .empty {
  padding: 3rem 1rem;
  text-align: center;
  color: var(--meta-color);
}

.feed-row {
  margin-top: 2rem;
}

.feed-row__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 0.5rem 0.5rem;
}

.feed-row__title {
  font-size: 1.05rem;
  font-weight: 700;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.feed-row__title-favicon {
  width: 20px; height: 20px;
  border-radius: 4px;
  object-fit: contain;
}

.feed-row__title-monogram {
  display: inline-flex;
  width: 20px; height: 20px;
  border-radius: 4px;
  color: #fff;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  font-weight: 700;
}

.feed-row__see-all {
  font-size: 0.85rem;
  color: var(--meta-color);
  text-decoration: none;
}

.feed-row__see-all:hover {
  color: var(--text-color);
}

/* Latest section — separated by border and wider bottom margin, no tint */
.latest-section {
  padding-top: 0.75rem;
  padding-bottom: 1.5rem;
  margin-bottom: 2.75rem;
  border-bottom: 1px solid var(--border-color);
}

/* Scroller wrapper: holds track + arrows + edge gradients */
.feed-row__scroller {
  position: relative;
}

.feed-row__scroller::before,
.feed-row__scroller::after {
  content: "";
  position: absolute;
  top: 0; bottom: 1rem;
  width: 40px;
  pointer-events: none;
  z-index: 2;
  opacity: 0;
  transition: opacity 0.25s ease;
}

.feed-row__scroller::before {
  left: 0;
  background: linear-gradient(to right, var(--bg-color), transparent);
}

.feed-row__scroller::after {
  right: 0;
  background: linear-gradient(to left, var(--bg-color), transparent);
}

.feed-row__scroller[data-at-start="false"]::before { opacity: 1; }
.feed-row__scroller[data-at-end="false"]::after { opacity: 1; }

.feed-row__arrow {
  position: absolute;
  top: calc(50% - 0.5rem);
  transform: translateY(-50%);
  width: 42px; height: 42px;
  border-radius: 50%;
  border: 0;
  background: var(--card-bg, #fff);
  color: var(--text-color);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.18);
  cursor: pointer;
  font-size: 1.4rem;
  line-height: 1;
  z-index: 3;
  opacity: 0;
  transition: opacity 0.2s ease, transform 0.12s ease, background 0.12s ease;
  display: flex; align-items: center; justify-content: center;
}

.feed-row__arrow:hover {
  transform: translateY(-50%) scale(1.08);
}

.feed-row__arrow:active {
  transform: translateY(-50%) scale(0.94);
}

.feed-row__arrow--prev { left: 6px; }
.feed-row__arrow--next { right: 6px; }

.feed-row__scroller:hover .feed-row__arrow:not(:disabled) {
  opacity: 1;
}

.feed-row__arrow:disabled {
  opacity: 0 !important;
  pointer-events: none;
}

@media (hover: none) {
  .feed-row__arrow { display: none; }
}

/* Horizontal scroll track */
.feed-row__track {
  display: flex;
  gap: 1.1rem;
  overflow-x: auto;
  overflow-y: hidden;
  scroll-snap-type: x mandatory;
  scroll-padding-inline: 0.75rem;
  scroll-behavior: smooth;
  overscroll-behavior-x: contain;
  -webkit-overflow-scrolling: touch;
  padding: 0.5rem 0.75rem 1.25rem;
  scrollbar-width: none;
}

.feed-row__track::-webkit-scrollbar { display: none; }

/* Article card (landscape — sender rows) */
.article-card {
  flex: 0 0 260px;
  scroll-snap-align: start;
  position: relative;
  display: flex;
  flex-direction: column;
  border-radius: 10px;
  overflow: hidden;
  text-decoration: none;
  color: inherit;
  background: var(--card-bg, #fff);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  transition: transform 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), box-shadow 0.18s ease;
}

.article-card:hover {
  transform: translateY(-3px) scale(1.02);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
}

.article-card:active {
  transform: translateY(-1px) scale(1);
}

.article-card__thumb {
  aspect-ratio: 16 / 9;
  background: #f0f0f0;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}

.article-card__thumb img {
  width: 100%; height: 100%; object-fit: cover;
}

.article-card__favicon {
  width: 40px !important;
  height: 40px !important;
  object-fit: contain !important;
}

.article-card__monogram {
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  font-size: 3rem; font-weight: 700; color: #fff;
  letter-spacing: -0.02em;
}

.article-card__body {
  padding: 0.65rem 0.75rem 0.85rem;
  position: relative;
}

.article-card__subject {
  font-size: 0.9rem;
  font-weight: 500;
  margin: 0 0 0.35rem;
  line-height: 1.3;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 2.35rem;
  padding-right: 1rem;
}

.article-card__date {
  font-size: 0.75rem;
  color: var(--meta-color);
  margin: 0;
}

.article-card__unread-dot {
  position: absolute;
  right: 0.75rem;
  bottom: 0.85rem;
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--unread-dot, #0066cc);
  box-shadow: 0 0 0 3px var(--card-bg, #fff);
}

/* Portrait card (Latest section only) */
.article-card--portrait {
  flex: 0 0 200px;
  border-radius: 12px;
}

.article-card--portrait .article-card__thumb {
  aspect-ratio: 4 / 5;
  flex: 0 0 auto;
}

.article-card--portrait .article-card__body {
  padding: 0.6rem 0.75rem 0.75rem;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.article-card__sender-label {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.7rem;
  color: var(--meta-color);
  margin: 0;
  line-height: 1;
}

.article-card__sender-label img {
  width: 14px; height: 14px;
  border-radius: 3px;
  object-fit: contain;
}

.article-card--portrait .article-card__subject {
  font-size: 0.88rem;
  font-weight: 500;
  margin: 0;
  line-height: 1.3;
  -webkit-line-clamp: 3;
  min-height: 3.4rem;
  padding-right: 0;
}

.article-card--portrait .article-card__date {
  margin-top: auto;
}

.article-card--portrait .article-card__unread-dot {
  top: 0.6rem;
  right: 0.6rem;
  bottom: auto;
  width: 10px; height: 10px;
  box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.9);
}

/* Dark mode overrides for the landing */
@media (prefers-color-scheme: dark) {
  .article-card {
    background: #1e1e1e;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  }
  .article-card:hover {
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.5);
  }
  .article-card__thumb { background: #2a2a2a; }
  .article-card--portrait .article-card__unread-dot {
    box-shadow: 0 0 0 3px rgba(30, 30, 30, 0.9);
  }
  .feed-row__arrow {
    background: #2a2a2a;
    color: #eee;
  }
  .feed-row__arrow:hover { background: #333; }
}

/* Ensure --card-bg and --unread-dot are defined in the :root already used
   elsewhere in this file. If they aren't, fall back is baked into the
   individual rules above. */
```

Also ensure `:root` at the top of the file has `--card-bg` and `--unread-dot`. Edit the existing `:root` block:

```css
:root {
    --bg-color: #ffffff;
    --text-color: #333333;
    --meta-color: #666666;
    --border-color: #e0e0e000;
    --link-color: #0066cc;
    --code-bg: #f5f5f5;
    --card-bg: #ffffff;
    --unread-dot: #0066cc;
}
```

And in `@media (prefers-color-scheme: dark) { :root { ... } }`:

```css
    --card-bg: #1e1e1e;
    --unread-dot: #4da6ff;
```

- [ ] **Step 6.2: Run tests**

```bash
poetry run pytest -v
poetry run ruff check .
```

Expected: tests pass, ruff clean.

- [ ] **Step 6.3: Commit**

```bash
git add static/reader.css
git commit -m "feat: landing-page CSS (feed rows, scroller, cards, latest section)"
```

---

## Task 7: JavaScript for scroller arrows

**Files:**
- Modify: `static/reader.js`

- [ ] **Step 7.1: Append scroller wiring to `static/reader.js`**

Append to the **end** of `static/reader.js` as a **new top-level** `DOMContentLoaded` handler. (The existing handler in sub-project 4 has an `if (!article) return;` early-return guard for article pages — we can't put scroller code inside it, since the landing page has no `.article[data-feed]` element. A separate handler is the cleanest split.)

```js
// --- Landing page scroller wiring (Netflix-style horizontal rows) ---
// For each .feed-row__scroller: drive arrows, edge-fade opacity via data attrs.
// Runs on / (landing) only; has no effect on other pages.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.feed-row__scroller').forEach((scroller) => {
    const track = scroller.querySelector('.feed-row__track');
    const prev = scroller.querySelector('.feed-row__arrow--prev');
    const next = scroller.querySelector('.feed-row__arrow--next');
    if (!track || !prev || !next) return;

    function cardStep() {
      const firstCard = track.querySelector('.article-card');
      if (!firstCard) return 260;
      // first card width + flex gap (1.1rem ≈ 18px)
      return firstCard.offsetWidth + 18;
    }

    function updateState() {
      const atStart = track.scrollLeft <= 1;
      const atEnd = track.scrollLeft + track.clientWidth >= track.scrollWidth - 1;
      scroller.dataset.atStart = atStart;
      scroller.dataset.atEnd = atEnd;
      prev.disabled = atStart;
      next.disabled = atEnd;
    }

    prev.addEventListener('click', () => {
      track.scrollBy({ left: -cardStep() * 2, behavior: 'smooth' });
    });
    next.addEventListener('click', () => {
      track.scrollBy({ left: cardStep() * 2, behavior: 'smooth' });
    });
    track.addEventListener('scroll', updateState, { passive: true });
    window.addEventListener('resize', updateState);
    requestAnimationFrame(updateState);
  });
});
```

This is a second `DOMContentLoaded` handler appended after the existing article-page handler. The existing handler's `if (!article) return;` early return means landing-page elements would be skipped if we merged them; keeping them in a separate handler is clearer and has no effect on existing article-page behavior.

- [ ] **Step 7.2: Sanity-check the JS loads**

```bash
poetry run pytest -v
```

Expected: no test regressions (no tests directly exercise JS).

Manual check: start the app locally (`poetry run python start.py`), open `/`, open DevTools console — no errors; hover a row → arrows appear; click next → track scrolls smoothly.

If running the app requires additional env vars/data, skip the manual check and rely on the existing `test_home_route_renders_feed_grid` to verify the `<script>` tag reaches the rendered HTML.

- [ ] **Step 7.3: Commit**

```bash
git add static/reader.js
git commit -m "feat: scroller arrow + edge-fade wiring in reader.js"
```

---

## Task 8: Smoke + README

**Files:**
- Modify: `README.md`

- [ ] **Step 8.1: Full test suite**

```bash
poetry run pytest -v
```

Expected: ~150 tests pass.

- [ ] **Step 8.2: Lint**

```bash
poetry run ruff check .
```

Expected: All checks passed.

- [ ] **Step 8.3: Docker builds**

```bash
docker build -f Dockerfile.serve -t email2rss-serve:sp5 .
docker build -f Dockerfile.fetch_and_generate -t email2rss-fetch:sp5 .
```

Expected: both succeed. If docker is unavailable on the dev machine, note as DONE_WITH_CONCERNS — CI will verify.

- [ ] **Step 8.4: Update `README.md`**

Find the existing "Accessing Your Feeds" or similar section (near the RSS/OPML docs). Add a new subsection:

```markdown
### Landing Page

- **`/`** — Browsable landing with per-newsletter rows of article cards (Netflix-style). Each row scrolls horizontally (touch-swipe friendly; arrow buttons on desktop hover). A "Latest" section at the top mixes the 10 newest articles across all feeds.
- **`/list`** — The plain XML-file list (the pre-redesign `/` behavior). Useful for grabbing raw `/<feed>.xml` URLs into an RSS reader.
- **`/<feed>.xml`** — Unchanged. RSS readers and direct-URL bookmarks are unaffected by the redesign.
```

- [ ] **Step 8.5: Commit docs**

```bash
git add README.md
git commit -m "docs: note new landing page + /list route"
```

---

## Acceptance criteria checklist

- [ ] `poetry run pytest -v` — ~150 tests pass
- [ ] `poetry run ruff check .` — clean
- [ ] Docker builds succeed
- [ ] Manual: hit `/`, see Latest (portrait cards) + per-sender rows (landscape cards). Scroll horizontally — smooth, snap, arrows appear on hover, edge fades toggle with scroll position.
- [ ] Manual: click a Latest card → opens `/article/<feed>/<guid>`.
- [ ] Manual: click a sender row's "See all →" → opens `/article/<feed>`.
- [ ] Manual: view `/list` → plain `<ul>` of XML filenames; each link goes to `/<feed>.xml`.
- [ ] Manual: view source on `/` — `<img>` thumbnails point at `/img?u=...&sig=...` signed URLs.
- [ ] Manual on mobile: swipe each row → momentum scroll, snap. No arrow buttons.
- [ ] Manual: `/<feed>.xml` behaves identically (RSS unaffected).
- [ ] First run with upgraded `emails.db` → migration adds `preview_image_url` and backfills from existing rows.
