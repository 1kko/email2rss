#!/usr/bin/env python3
"""
A Simple python webserver which serves only `rss_feed` folder and only xml files.
"""
from __future__ import annotations

import os
import functools
import http.server

from common import logging, config


class SimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    A simple HTTP request handler that serves files from a specified directory.
    """

    def __init__(self, *args, **kwargs):
        directory = kwargs.get("directory", "rss_feed")
        kwargs.pop("directory", None)
        super().__init__(*args, directory=directory, **kwargs)


def run(
    server_class=http.server.HTTPServer,
    handler_class=SimpleHTTPRequestHandler,
    directory="rss_feed",
    port=8000,
):
    """
    Run an HTTP server to serve static files from a specified directory.

    Args:
        server_class (class): The HTTP server class to use. Defaults to http.server.HTTPServer.
        handler_class (class): The request handler class to use.
                               Defaults to SimpleHTTPRequestHandler.
        directory (str): The directory from which to serve the static files.
                         Defaults to "rss_feed".
        port (int): The port number on which to run the server. Defaults to 8000.
    """
    server_address = ("", port)

    # Create a partial function that initializes the handler with the directory
    handler = functools.partial(handler_class, directory=directory)

    httpd = server_class(server_address, handler)
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
    directory = config.get("directory", "rss_feed")
    port = config.get("port", 8000)

    # Ensure the directory exists
    if not os.path.exists(directory):
        os.makedirs(directory)
    run(directory=directory, port=port)


if __name__ == "__main__":
    main()
