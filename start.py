#!/usr/bin/env python3
"""
Runs two multi-processes to fetch emails and generate RSS feeds, 
and serve the feeds as an HTTP server.
"""


import multiprocessing
import time

import feed_converter
import static_serve


def fetch_and_generate():
    """
    Fetches and generates RSS feed from emails periodically.

    This function continuously calls the `feed_converter.main()` function to
    fetch and generate an RSS feed from emails. It then sleeps for 120 seconds
    before repeating the process.

    Returns:
        None
    """
    while True:
        feed_converter.main()
        time.sleep(120)


def serve():
    """
    Starts the server and serves the static files.
    """
    static_serve.main()


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
