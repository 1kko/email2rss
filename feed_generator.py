#!/usr/bin/env python3

"""Retrieve emails from database and convert them to an RSS feed.

This script connects to database, fetches emails,
and converts them into an RSS feed. The RSS feed is then saved to a file.

"""

from __future__ import annotations

import email
import email.header
import hashlib

from pathlib import Path
from feedgen.feed import FeedGenerator


import database as db
from common import logging, config
from util import (
    extract_email_address,
    extract_name_from_email,
    extract_domain_address,
    utf8_decoder,
    cleanse_content,
)


def generate_rss(sender, messages):
    """
    Generate an RSS feed for emails from a specific sender.

    Args:
        sender (str): The email sender's address.
        messages (list): A list of email messages.

    Returns:
        str: The generated RSS feed as a string.

    Raises:
        Exception: If there is an error generating the RSS feed.
    """
    try:
        channel = FeedGenerator()
        channel.link(href=f"https://{extract_domain_address(sender)}", rel="alternate")
        channel.description(f"RSS feed for {sender}")

        channel_data = {"name": sender, "pubDate": None}

        for mail_item in messages:
            msg = email.message_from_bytes(mail_item.content)
            feed_entry = channel.add_entry()

            title = email.header.make_header(email.header.decode_header(msg["subject"]))
            feed_entry.title(str(title))

            feed_entry.link(href=f"https://{extract_domain_address(sender)}")

            feed_entry.published(email.utils.parsedate_to_datetime(msg["date"]))

            feed_entry.updated(email.utils.parsedate_to_datetime(msg["date"]))

            # Update the channel_data['pubDate'] to the latest email date
            if (
                channel_data.get("pubDate") is None
                or channel_data.get("pubDate") < feed_entry.published()
            ):
                channel_data["pubDate"] = feed_entry.published()

            channel_name = email.utils.parseaddr(msg["from"])[0]
            if channel_name:
                channel_data["name"] = channel_name

            unique_string = msg["subject"] + msg["date"] + msg["from"]
            guid = hashlib.md5(unique_string.encode()).hexdigest()
            feed_entry.id(guid)

            feed_entry.author(
                {
                    "name": utf8_decoder(extract_name_from_email(msg["from"])),
                    "email": extract_email_address(sender),
                }
            )

            content = ""
            html_content = None
            if msg.is_multipart():
                for part in msg.walk():
                    c_type = part.get_content_type()
                    c_disp = str(part.get("Content-Disposition"))
                    if "attachment" not in c_disp:
                        charset = (
                            part.get_content_charset() or "utf-8"
                        )  # Default charset to utf-8
                        if c_type == "text/html":
                            html_content = cleanse_content(
                                part.get_payload(decode=True).decode(
                                    charset, errors="ignore"
                                )
                            )
                        elif c_type == "text/plain" and html_content is None:
                            content = cleanse_content(
                                part.get_payload(decode=True).decode(
                                    charset, errors="ignore"
                                )
                            )
            else:
                charset = msg.get_content_charset() or "utf-8"
                if msg.get_content_type() == "text/html":
                    html_content = cleanse_content(
                        msg.get_payload(decode=True).decode(charset, errors="ignore")
                    )
                elif msg.get_content_type() == "text/plain":
                    content = cleanse_content(
                        msg.get_payload(decode=True).decode(charset, errors="ignore")
                    )

            # Prefer HTML content if available
            feed_entry.description(
                html_content if html_content is not None else content
            )

        channel.title(utf8_decoder(channel_data.get("name")))
        channel.pubDate(channel_data.get("pubDate"))

        logging.info(f"Generated RSS feed for {sender}.")
        return channel.rss_str(pretty=True).decode("utf-8")
    except Exception as e:
        logging.error(f"Failed to generate RSS feed for {sender}: {e}")
        raise


def save_feed(sender, feed_content, save_path="rss_feed"):
    """
    Saves the RSS feed content to a file.

    Args:
        sender (str): The email address of the sender.
        feed_content (str): The content of the RSS feed.

    Returns:
        str: The filename of the saved RSS feed.

    Raises:
        Exception: If there is an error while saving the RSS feed.

    """
    try:
        # in case email address extraction fails, use a default value
        email_address = extract_email_address(
            sender, default="not_avail@unknown_email.com"
        )

        # Sanitize the email address to be safe for use as a filename
        # while it is totally fine to use email address as filename
        # we are serving these static files to web, so it is better to sanitize it
        sanitized_email = email_address.replace("@", "_").replace(".", "_")
        # create "rss_feed" folder if not exists
        output_dir = Path(save_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        xml_filename = Path(f"{sanitized_email}.xml")
        save_path = output_dir / xml_filename

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(feed_content)
        logging.info(f"{sender} Saved RSS feed to file: {save_path}")
        return save_path
    except Exception as e:
        logging.error(f"{sender} Failed to save RSS feed: {e}")
        raise


def main():
    """Entry point of the email to RSS feed converter.
    Reads email messages from the database, generates an RSS feed for each sender,
    and saves the feed to a file.
    """

    data_dir = Path(config.get("data_dir"))
    data_feed_dir = data_dir / "feed"

    for sender in db.get_senders():
        messages = db.get_email(sender)
        logging.info(f"{sender} found entries={messages.count()}")
        rss_feed = generate_rss(sender, messages)
        _ = save_feed(sender, rss_feed, save_path=data_feed_dir)


# Main Execution
if __name__ == "__main__":
    main()
