#!/usr/bin/env python3
"""
Flask app serving RSS feeds, OPML subscription files, and the optional internal reader.
"""
from __future__ import annotations

import email as email_mod
import email.header
import os
import ssl
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, send_from_directory

import database as db
from common import logging, config
from util import cleanse_content, sanitize_html


PROJECT_ROOT = Path(__file__).parent
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
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


def _attach_feed_names(articles):
    return [{**a, "feed_name": sanitize_feed_name(a["sender"])} for a in articles]


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
        template_folder=str(TEMPLATE_DIR),
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
        articles_raw = db.get_all_emails_with_metadata()
        senders: dict = {}
        for article in articles_raw:
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

        sorted_senders = sorted(senders.items(), key=lambda x: x[1]["latest"], reverse=True)
        return render_template(
            "article_list.html",
            page_title="All Articles",
            articles=_attach_feed_names(articles_raw),
            senders=sorted_senders,
            specific_sender=None,
        )

    @app.get("/article/<feed_name>")
    def feed_article_list(feed_name):
        sender_email = feed_name_to_email(feed_name)
        articles_raw = db.get_emails_by_sender_with_metadata(sender_email)
        if not articles_raw:
            abort(404)
        return render_template(
            "article_list.html",
            page_title=f"Articles from {sender_email}",
            articles=_attach_feed_names(articles_raw),
            senders=None,
            specific_sender=sender_email,
        )

    @app.get("/article/<feed_name>/<guid>")
    def view_article(feed_name, guid):
        sender_email = feed_name_to_email(feed_name)
        try:
            record = db.get_email_by_guid(sender_email, guid)
            if not record:
                abort(404)

            msg = email_mod.message_from_bytes(record.content)
            subject = str(email_mod.header.make_header(email_mod.header.decode_header(msg["subject"])))
            return render_template(
                "article.html",
                subject=subject,
                sender=sender_email,
                date=msg["date"] or "",
                content=extract_article_content(msg),
            )
        except Exception:
            logging.exception(f"Error serving article {feed_name}/{guid}")
            abort(500)

    @app.get("/")
    def index():
        try:
            entries = sorted(
                p.name for p in FEED_DIR.iterdir()
                if p.is_file() and not p.name.endswith(".db")
            )
        except FileNotFoundError:
            entries = []
        return render_template(
            "index.html",
            entries=entries,
            enable_internal_reader=bool(config.get("enable_internal_reader")),
        )

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
