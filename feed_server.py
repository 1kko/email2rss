#!/usr/bin/env python3
"""
Flask app serving RSS feeds, OPML subscription files, and the optional internal reader.
"""
from __future__ import annotations

import email as email_mod
import email.header
import html as html_module
import os
import ssl
from pathlib import Path

from flask import Flask, abort, jsonify, send_from_directory

import database as db
from common import logging, config
from util import cleanse_content, sanitize_html


PROJECT_ROOT = Path(__file__).parent
STATIC_DIR = PROJECT_ROOT / "static"
FEED_DIR = (Path(config.get("data_dir", "data")) / "feed").resolve()


def sanitize_feed_name(email_address: str) -> str:
    return email_address.replace("@", "_").replace(".", "_")


def feed_name_to_email(feed_name: str) -> str:
    parts = feed_name.split("_")
    if len(parts) >= 2:
        return parts[0] + "@" + ".".join(parts[1:])
    return feed_name.replace("_", "@", 1).replace("_", ".")


def extract_article_content(msg) -> str:
    html_content: str | None = None
    text_content: str | None = None

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition"))
            if "attachment" in content_disposition:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                decoded = payload.decode(charset, errors="ignore")
            except Exception:
                logging.debug("Skipping unparseable MIME part")
                continue
            ctype = part.get_content_type()
            if ctype == "text/html":
                html_content = cleanse_content(decoded)
            elif ctype == "text/plain" and html_content is None:
                text_content = cleanse_content(decoded)
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(charset, errors="ignore")
            if msg.get_content_type() == "text/html":
                html_content = cleanse_content(decoded)
            elif msg.get_content_type() == "text/plain":
                text_content = cleanse_content(decoded)

    content = html_content if html_content else (text_content or "")
    if not html_content and text_content:
        content = f"<pre>{content}</pre>"
    return sanitize_html(content)


def generate_article_html(subject: str, sender: str, date: str, content: str) -> str:
    escaped_subject = html_module.escape(subject)
    escaped_sender = html_module.escape(sender)
    escaped_date = html_module.escape(date or "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escaped_subject}</title>
    <link rel="stylesheet" href="/static/reader.css">
</head>
<body>
    <article>
        <header>
            <h1>{escaped_subject}</h1>
            <p class="meta">From: {escaped_sender} | Date: {escaped_date}</p>
        </header>
        <div class="content">
            {content}
        </div>
    </article>
    <script src="/static/reader.js"></script>
</body>
</html>"""


def generate_article_list_html(articles, senders=None, specific_sender=None) -> str:
    if specific_sender:
        escaped_sender = html_module.escape(specific_sender)
        page_title = f"Articles from {escaped_sender}"
        header_html = f"""
            <header>
                <h1>Articles from {escaped_sender}</h1>
                <p class="meta">
                    Total articles: {len(articles)} |
                    <a href="/article" style="color: var(--link-color);">View all feeds</a>
                </p>
            </header>
        """
    else:
        total_articles = len(articles)
        total_feeds = len(senders) if senders else 0
        page_title = "All Articles"

        feed_stats_html = ""
        if senders:
            feed_stats_html = "<div class='feed-stats'><h2>Feeds</h2><ul class='feed-list'>"
            for sender, stats in sorted(senders.items(), key=lambda x: x[1]["latest"], reverse=True):
                last_updated = stats["latest"].strftime("%Y-%m-%d %H:%M")
                feed_stats_html += f"""
                    <li>
                        <a href="/article/{stats['feed_name']}">{html_module.escape(sender)}</a>
                        <span class="meta">({stats['count']} articles, last updated: {last_updated})</span>
                    </li>
                """
            feed_stats_html += "</ul></div>"

        header_html = f"""
            <header>
                <h1>All Articles</h1>
                <p class="meta">Total feeds: {total_feeds} | Total articles: {total_articles}</p>
            </header>
            {feed_stats_html}
        """

    articles_html = "<div class='article-list'><h2>Recent Articles</h2><ul class='article-items'>"
    for article in articles:
        feed_name = sanitize_feed_name(article["sender"])
        article_url = f"/article/{feed_name}/{article['guid']}"
        articles_html += f"""
            <li class='article-item'>
                <a href="{article_url}" class='article-title'>{html_module.escape(article['subject'])}</a>
                <div class="meta">
                    From: <a href="/article/{feed_name}">{html_module.escape(article['sender'])}</a> |
                    Date: {html_module.escape(article['date'] or '')}
                </div>
            </li>
        """
    articles_html += "</ul></div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title}</title>
    <link rel="stylesheet" href="/static/reader.css">
    <style>
        .feed-stats {{ margin: 2rem 0; padding: 1.5rem; background-color: var(--code-bg); border-radius: 8px; }}
        .feed-stats h2 {{ margin-bottom: 1rem; font-size: 1.5rem; }}
        .feed-list {{ list-style: none; padding: 0; }}
        .feed-list li {{ padding: 0.75rem 0; border-bottom: 1px solid var(--border-color); }}
        .feed-list li:last-child {{ border-bottom: none; }}
        .feed-list a {{ color: var(--link-color); text-decoration: none; font-weight: 600; font-size: 1.1rem; }}
        .feed-list a:hover {{ text-decoration: underline; }}
        .article-list {{ margin: 2rem 0; }}
        .article-list h2 {{ margin-bottom: 1rem; font-size: 1.5rem; }}
        .article-items {{ list-style: none; padding: 0; }}
        .article-item {{ padding: 1rem 0; border-bottom: 1px solid var(--border-color); }}
        .article-item:last-child {{ border-bottom: none; }}
        .article-title {{ color: var(--link-color); text-decoration: none; font-weight: 600; font-size: 1.2rem; display: block; margin-bottom: 0.5rem; }}
        .article-title:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <article>
        {header_html}
        {articles_html}
    </article>
</body>
</html>"""


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src * data:; style-src 'self' 'unsafe-inline'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.before_request
    def _log_request():
        from flask import request
        safe_path = request.path.replace("\n", "").replace("\r", "")
        logging.info(f"Serving ip={request.remote_addr} path={safe_path}")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/stats")
    def stats():
        senders = db.get_senders()
        return jsonify({
            "total_emails": db.get_entry_count(),
            "total_senders": len(senders),
            "senders": senders,
        })

    @app.get("/article")
    def article_list():
        articles = db.get_all_emails_with_metadata()
        senders: dict = {}
        for article in articles:
            sender = article["sender"]
            if sender not in senders:
                senders[sender] = {
                    "count": 0,
                    "latest": article["timestamp"],
                    "feed_name": sanitize_feed_name(sender),
                }
            senders[sender]["count"] += 1
            if article["timestamp"] > senders[sender]["latest"]:
                senders[sender]["latest"] = article["timestamp"]
        return generate_article_list_html(articles, senders, None)

    @app.get("/article/<feed_name>")
    def feed_article_list(feed_name):
        sender_email = feed_name_to_email(feed_name)
        articles = db.get_emails_by_sender_with_metadata(sender_email)
        if not articles:
            abort(404)
        return generate_article_list_html(articles, None, sender_email)

    @app.get("/article/<feed_name>/<guid>")
    def view_article(feed_name, guid):
        sender_email = feed_name_to_email(feed_name)
        try:
            record = db.get_email_by_guid(sender_email, guid)
            if not record:
                abort(404)

            msg = email_mod.message_from_bytes(record.content)
            subject = str(email_mod.header.make_header(email_mod.header.decode_header(msg["subject"])))
            date_text = msg["date"]
            content = extract_article_content(msg)
            return generate_article_html(subject, sender_email, date_text, content)
        except Exception:
            logging.exception(f"Error serving article {feed_name}/{guid}")
            abort(500)

    @app.get("/")
    def index():
        try:
            entries = sorted(p.name for p in FEED_DIR.iterdir() if p.is_file() and not p.name.endswith(".db"))
        except FileNotFoundError:
            entries = []

        items = "\n".join(f'<li><a href="/{html_module.escape(name)}">{html_module.escape(name)}</a></li>' for name in entries)
        reader_link = '<p><a href="/article">Open internal reader</a></p>' if config.get("enable_internal_reader") else ""
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>email2rss</title></head>
<body>
<h1>email2rss</h1>
{reader_link}
<ul>{items}</ul>
</body></html>"""

    @app.get("/<path:filename>")
    def serve_feed_file(filename):
        if filename.endswith(".db") or filename.startswith("."):
            abort(404)
        try:
            return send_from_directory(str(FEED_DIR), filename)
        except FileNotFoundError:
            abort(404)

    return app


app = create_app()


def main():
    logging.basicConfig(level=logging.INFO)

    FEED_DIR.mkdir(parents=True, exist_ok=True)

    port = config.get("port")
    bind_address = config.get("bind_address", "127.0.0.1")
    certfile = os.getenv("certfile")
    keyfile = os.getenv("keyfile")

    ssl_context = None
    if certfile and keyfile:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(certfile, keyfile)

    logging.info(f"Serving {FEED_DIR}/ on http://{bind_address}:{port}/")
    app.run(host=bind_address, port=port, ssl_context=ssl_context, threaded=True)


if __name__ == "__main__":
    main()
