#!/usr/bin/env python3
"""
A Simple python webserver which serves only `rss_feed` folder and only xml files.
"""
from __future__ import annotations
import functools
import http.server
from common import logging, config
import os


class SimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # directory = kwargs.get("directory", "rss_feed")
        directory = kwargs.get("directory", "rss_feed")
        kwargs.pop("directory", None)
        super().__init__(*args, directory=directory, **kwargs)


def run(
    server_class=http.server.HTTPServer,
    handler_class=SimpleHTTPRequestHandler,
    directory="rss_feed",
    port=8000,
):
    server_address = ("", port)

    # Create a partial function that initializes the handler with the directory
    handler = functools.partial(handler_class, directory=directory)

    httpd = server_class(server_address, handler)
    logging.info(
        f"Serving HTTP on 0.0.0.0 port {port} (http://0.0.0.0:{port}/) serving files from {directory}/"
    )
    httpd.serve_forever()


def main():
    directory = config.get("directory", "rss_feed")
    port = config.get("port", 8000)

    # Ensure the directory exists
    if not os.path.exists(directory):
        os.makedirs(directory)
    run(directory=directory, port=port)


if __name__ == "__main__":
    main()
