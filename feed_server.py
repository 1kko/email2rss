#!/usr/bin/env python3
"""
Flask app serving RSS feeds, OPML subscription files, and the optional internal reader.
"""
from __future__ import annotations

import base64
import email as email_mod
import email.header
import os
import ssl
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

import database as db
import img_proxy
import reader
from common import logging, config, get_img_proxy_secret


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
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-src 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.before_request
    def _log_request():
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
        filter_mode = request.args.get("filter", "all")
        if filter_mode not in ("all", "unread", "starred"):
            abort(400)
        articles = db.get_emails_filtered(
            sender=None, filter_mode=filter_mode, limit=config.get("max_item_per_feed", 100) * 10
        )
        # Group by sender for the sidebar
        senders: dict = {}
        for article in articles:
            s = article["sender"]
            if s not in senders:
                senders[s] = {"count": 0, "latest": article["timestamp"], "feed_name": article["feed_name"]}
            senders[s]["count"] += 1
            if article["timestamp"] > senders[s]["latest"]:
                senders[s]["latest"] = article["timestamp"]
        sorted_senders = sorted(senders.items(), key=lambda x: x[1]["latest"], reverse=True)

        return render_template(
            "article_list.html",
            page_title="All Articles",
            articles=articles,
            senders=sorted_senders,
            specific_sender=None,
            filter_mode=filter_mode,
        )

    @app.get("/article/<feed_name>")
    def feed_article_list(feed_name):
        sender_email = feed_name_to_email(feed_name)
        filter_mode = request.args.get("filter", "all")
        if filter_mode not in ("all", "unread", "starred"):
            abort(400)
        articles = db.get_emails_filtered(
            sender=sender_email, filter_mode=filter_mode,
            limit=config.get("max_item_per_feed", 100),
        )
        if not articles and filter_mode == "all":
            abort(404)
        return render_template(
            "article_list.html",
            page_title=f"Articles from {sender_email}",
            articles=articles,
            senders=None,
            specific_sender=sender_email,
            filter_mode=filter_mode,
        )

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        error = None
        results = []
        if query:
            try:
                results = db.search_emails(query, limit=50)
            except db.SearchSyntaxError as e:
                error = str(e)
        return render_template(
            "search_results.html",
            query=query,
            results=results,
            error=error,
            search_q=query,
        )

    @app.get("/article/<feed_name>/<guid>")
    def view_article(feed_name, guid):
        sender_email = feed_name_to_email(feed_name)
        try:
            record = db.get_email_by_guid(sender_email, guid)
            if not record:
                abort(404)

            msg = email_mod.message_from_bytes(record.content)
            subject = str(email_mod.header.make_header(
                email_mod.header.decode_header(msg["subject"])
            ))
            body_html, cid_map = reader.extract_body_and_cid_map(msg)

            proxy_base = (config.get("server_baseurl") or "").rstrip("/")
            proxy_origin = proxy_base  # origin = baseurl for signed /img
            secret = get_img_proxy_secret()

            def _sign(url):
                return img_proxy.sign_url(url, secret, proxy_base)

            cleaned = reader.clean_and_rewrite(body_html, cid_map, _sign)
            iframe_document = reader.render_iframe_document(cleaned, proxy_origin)

            # State attributes are already loaded on `record` (eager via get_email_by_guid's .all())
            is_read = record.is_read
            is_starred = record.is_starred

            return render_template(
                "article.html",
                subject=subject,
                sender=sender_email,
                date=msg["date"] or "",
                iframe_document=iframe_document,
                feed_name=feed_name,
                guid=guid,
                is_read=is_read,
                is_starred=is_starred,
                read_after_seconds=config.get("read_after_seconds", 5),
            )
        except HTTPException:
            raise
        except Exception:
            logging.exception(f"Error serving article {feed_name}/{guid}")
            abort(500)

    def _assert_same_origin():
        origin = request.headers.get("Origin")
        if not origin:
            return  # absent means non-browser caller (curl, test client) — allow
        baseurl = (config.get("server_baseurl") or "").rstrip("/")
        if baseurl and origin != baseurl:
            abort(403)

    @app.post("/article/<feed_name>/<guid>/read")
    def mark_article_read(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_read(record.id, True)
        return jsonify({"is_read": True})

    @app.delete("/article/<feed_name>/<guid>/read")
    def unmark_article_read(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_read(record.id, False)
        return jsonify({"is_read": False})

    @app.post("/article/<feed_name>/<guid>/star")
    def mark_article_starred(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_starred(record.id, True)
        return jsonify({"is_starred": True})

    @app.delete("/article/<feed_name>/<guid>/star")
    def unmark_article_starred(feed_name, guid):
        _assert_same_origin()
        sender_email = feed_name_to_email(feed_name)
        record = db.get_email_by_guid(sender_email, guid)
        if not record:
            abort(404)
        db.mark_starred(record.id, False)
        return jsonify({"is_starred": False})

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

    @app.get("/img")
    def image_proxy_route():
        u = request.args.get("u", "")
        sig = request.args.get("sig", "")
        if not u:
            abort(400)
        if not sig:
            abort(403)

        secret = get_img_proxy_secret()
        if not img_proxy.verify_signature(u, sig, secret):
            abort(403)

        try:
            pad = "=" * (-len(u) % 4)
            url = base64.urlsafe_b64decode(u + pad).decode("utf-8")
        except Exception:
            logging.warning("Failed to decode /img u param: %r", u)
            abort(400)

        body, ctype = img_proxy.fetch_image(url, secret)
        return Response(
            body,
            mimetype=ctype,
            headers={
                "Cache-Control": "public, max-age=604800",
                "Content-Security-Policy": "default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
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

    from common import validate_reader_config
    validate_reader_config()

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
