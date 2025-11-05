#!/usr/bin/env python3
"""
A Simple python web server which serves RSS feeds and provides an internal reader.
"""
from __future__ import annotations

import os
import functools
import http.server
import ssl
import email
import email.header
import mimetypes
from pathlib import Path
from urllib.parse import unquote

import database as db
from common import logging, config
from util import cleanse_content


class RSSRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    Enhanced HTTP request handler with routing for RSS feeds, static assets, and internal reader.
    """

    def __init__(self, *args, **kwargs):
        self.feed_directory = kwargs.get("directory")
        kwargs.pop("directory", None)
        super().__init__(*args, directory=self.feed_directory, **kwargs)

    def do_GET(self):
        """
        Serve a GET request with routing support.
        """
        # Block database files
        if self.path.endswith(".db"):
            self.send_error(404, "File not found")
            return

        logging.info(
            f"Serving ip={self.client_address[0]} headers={self.headers} {self.path}"
        )

        # Route handling
        path_parts = self.path.split("?")[0].strip("/").split("/")

        # Route: /article/{feed}/{guid}
        if len(path_parts) >= 3 and path_parts[0] == "article":
            self.serve_article(path_parts[1], path_parts[2])
            return

        # Route: /static/{filename}
        if len(path_parts) >= 2 and path_parts[0] == "static":
            self.serve_static_file(path_parts[1])
            return

        # Default: serve files from feed directory (RSS XML, OPML)
        super().do_GET()

    def serve_article(self, feed_name, guid):
        """
        Serve an article from the internal reader.

        Args:
            feed_name (str): Sanitized feed name (e.g., hello_tailscale_com)
            guid (str): MD5 GUID of the article
        """
        try:
            # Convert sanitized feed name back to email address
            # This is a best-effort conversion that works for most email addresses
            # The GUID matching ensures we get the correct email
            parts = feed_name.split("_")
            if len(parts) >= 2:
                # Assume format: localpart_domain_tld
                # First part is local, rest joined with dots form domain
                sender_email = parts[0] + "@" + ".".join(parts[1:])
            else:
                # Fallback for edge cases
                sender_email = feed_name.replace("_", "@", 1).replace("_", ".")

            # Retrieve email from database by GUID
            email_record = db.get_email_by_guid(sender_email, guid)

            if not email_record:
                self.send_error(404, "Article not found")
                return

            # Parse email content
            msg = email.message_from_bytes(email_record.content)

            # Extract subject
            subject = email.header.make_header(email.header.decode_header(msg["subject"]))
            subject_text = str(subject)

            # Extract date
            date_text = msg["date"]

            # Extract HTML content
            html_content = None
            text_content = None

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if "attachment" not in content_disposition:
                        charset = part.get_content_charset() or "utf-8"
                        payload = part.get_payload(decode=True)
                        if payload:
                            try:
                                payload_decoded = payload.decode(charset, errors="ignore")
                                if content_type == "text/html":
                                    html_content = cleanse_content(payload_decoded)
                                elif content_type == "text/plain" and html_content is None:
                                    text_content = cleanse_content(payload_decoded)
                            except Exception:
                                continue
            else:
                charset = msg.get_content_charset() or "utf-8"
                if msg.get_content_type() == "text/html":
                    html_content = cleanse_content(
                        msg.get_payload(decode=True).decode(charset, errors="ignore")
                    )
                elif msg.get_content_type() == "text/plain":
                    text_content = cleanse_content(
                        msg.get_payload(decode=True).decode(charset, errors="ignore")
                    )

            # Prefer HTML content, fallback to text
            content = html_content if html_content else text_content or ""

            # Convert plain text to HTML if needed
            if not html_content and text_content:
                content = f"<pre>{content}</pre>"

            # Generate HTML response
            html = self.generate_article_html(subject_text, sender_email, date_text, content)

            # Send response
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        except Exception as e:
            logging.error(f"Error serving article {feed_name}/{guid}: {e}")
            self.send_error(500, f"Internal server error: {str(e)}")

    def serve_static_file(self, filename):
        """
        Serve static assets from the static/ directory.

        Args:
            filename (str): Name of the static file to serve
        """
        try:
            # Get the project root directory (parent of feed_server.py)
            project_root = Path(__file__).parent
            static_dir = project_root / "static"
            file_path = static_dir / filename

            # Security check: ensure file is within static directory
            if not str(file_path.resolve()).startswith(str(static_dir.resolve())):
                self.send_error(403, "Forbidden")
                return

            if not file_path.exists() or not file_path.is_file():
                self.send_error(404, "File not found")
                return

            # Determine MIME type
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if mime_type is None:
                mime_type = "application/octet-stream"

            # Read and serve file
            with open(file_path, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        except Exception as e:
            logging.error(f"Error serving static file {filename}: {e}")
            self.send_error(500, f"Internal server error: {str(e)}")

    def generate_article_html(self, subject, sender, date, content):
        """
        Generate HTML for article display.

        Args:
            subject (str): Email subject
            sender (str): Sender email address
            date (str): Email date
            content (str): Email HTML content

        Returns:
            str: Complete HTML page
        """
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
    <link rel="stylesheet" href="/static/reader.css">
</head>
<body>
    <article>
        <header>
            <h1>{subject}</h1>
            <p class="meta">From: {sender} | Date: {date}</p>
        </header>
        <div class="content">
            {content}
        </div>
    </article>
    <script src="/static/reader.js"></script>
</body>
</html>"""

    def log_message(self, format, *args):
        """
        Log an arbitrary message.

        This is used by all other logging functions.
        Override it to log messages to the logging module.
        """
        logging.info(f"{self.client_address[0]} - {format % args}")
        

def run(
    server_class=http.server.HTTPServer,
    handler_class=RSSRequestHandler,
    directory="data/feed",
    port=config.get("port"),
    certfile=None,
    keyfile=None,
):
    """
    Run an HTTP server to serve static files from a specified directory.

    Args:
        server_class (class): The HTTP server class to use. Defaults to http.server.HTTPServer.
        handler_class (class): The request handler class to use.
                               Defaults to SimpleHTTPRequestHandler.
        directory (str): The directory from which to serve the static files.
                         Defaults to "data/feed".
        port (int): The port number on which to run the server. Defaults to 8000.
    """
    server_address = ("", port)

    # Create a partial function that initializes the handler with the directory
    handler = functools.partial(handler_class, directory=directory)

    httpd = server_class(server_address, handler)

    # If certfile and keyfile are provided, run the server with SSL
    if certfile and keyfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    logging.info(f"Serving {directory}/ to HTTP http://0.0.0.0:{port}/")
    httpd.serve_forever()


def main():
    """
    This function is the entry point of the program.
    It creates a directory if it doesn't exist and starts the server.

    Parameters:
    - directory (str): The directory to serve the RSS feed from.
    - port (int): The port number to run the server on.

    Returns:
    None
    """
    # configure logging
    logging.basicConfig(level=logging.INFO)

    directory = os.path.join(config.get("data_dir"), "feed")
    port = config.get("port")

    # Ensure the directory exists
    if not os.path.exists(directory):
        os.makedirs(directory)
    run(directory=directory, port=port)


if __name__ == "__main__":
    main()
