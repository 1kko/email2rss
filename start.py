#!/usr/bin/env python3
"""
Runs two multi-processes to fetch emails and generate RSS feeds, 
and serve the feeds as an HTTP server.
"""


import multiprocessing
import time

import feed_converter
import feed_server
from common import logging, config


def fetch_and_generate():
    """
    Fetches and generates RSS feed from emails periodically.

    This function continuously calls the `feed_converter.main()` function to
    fetch and generate an RSS feed from emails. It then sleeps for 120 seconds
    before repeating the process.

    Returns:
        None
    """
    refresh_seconds = config.get("refresh_seconds", 300)
    while True:
        feed_converter.main()
        logging.info(f"waiting for {refresh_seconds} seconds")
        time.sleep(refresh_seconds)


def serve():
    """
    Starts the server and serves the static files.
    """
    feed_server.main()


if __name__ == "__main__":
    # Create processes
    process1 = multiprocessing.Process(target=fetch_and_generate)
    process2 = multiprocessing.Process(target=serve)

    # Start processes
    process1.start()
    process2.start()

    # Wait for both processes to finish
    process1.join()
    process2.join()
