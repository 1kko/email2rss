#!/usr/bin/env python3

"""Retrieve emails from database and convert them to an RSS feed.

This script connects to database, fetches emails,
and converts them into an RSS feed. The RSS feed is then saved to a file.

"""

from __future__ import annotations

import email
import email.header
import hashlib
import xml.etree.ElementTree as ET
import datetime
from pathlib import Path
from urllib.parse import urljoin
from feedgen.feed import FeedGenerator

import database as db
from common import config, logging
from util import (
    cleanse_content,
    extract_domain_address,
    extract_email_address,
    extract_name_from_email,
    utf8_decoder,
)


def add_base_url(url):
    """Add base URL to the URL if it is not already present."""
    if not url.startswith("http") and config.get("server_baseurl"):
        return urljoin(config.get("server_baseurl"), url)
    return url


def ensure_timezone(dt):
    """Ensure datetime object has timezone info."""
    if not dt.tzinfo:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


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

            # Process email subject
            title = email.header.make_header(email.header.decode_header(msg["subject"]))
            feed_entry.title(str(title))

            # Extract the domain address from the sender email address
            feed_entry.link(href=f"https://{extract_domain_address(sender)}")

            # Process email published and updated date
            dt = email.utils.parsedate_to_datetime(msg["date"])
            dt = ensure_timezone(dt)
            feed_entry.published(dt)
            feed_entry.updated(dt)

            # Update the channel_data['pubDate'] to the latest email date
            if channel_data.get("pubDate") is None or channel_data.get("pubDate") < dt:
                channel_data["pubDate"] = dt

            # Process email sender information
            channel_name = email.utils.parseaddr(msg["from"])[0]
            if channel_name:
                channel_data["name"] = channel_name

            # Generate unique GUID
            unique_string = msg["subject"] + msg["date"] + msg["from"]
            guid = hashlib.md5(unique_string.encode()).hexdigest()
            feed_entry.id(guid)

            # Process author information
            feed_entry.author({
                "name": utf8_decoder(extract_name_from_email(msg["from"])),
                "email": extract_email_address(sender),
            })

            # Process email content
            content = ""
            html_content = None
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if "attachment" not in content_disposition:
                        charset = (
                            part.get_content_charset() or "utf-8"
                        )  # Default charset to utf-8
                        payload = part.get_payload(decode=True)
                        if payload:
                            try:
                                payload_decoded = payload.decode(
                                    charset, errors="ignore"
                                )
                                if content_type == "text/html":
                                    html_content = cleanse_content(payload_decoded)
                                elif (
                                    content_type == "text/plain"
                                    and html_content is None
                                ):
                                    content = cleanse_content(
                                        part.get_payload(decode=True).decode(
                                            charset, errors="ignore"
                                        )
                                    )
                            except Exception as e:
                                logging.error(f"Failed to decode payload: {e}")
                                continue
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

            # Set entry description
            # Prefer HTML content if available
            feed_entry.description(
                html_content if html_content is not None else content
            )

        # Finalize channel metadata
        channel.title(utf8_decoder(channel_data.get("name")))
        if channel_data.get("pubDate"):
            channel.pubDate(ensure_timezone(channel_data.get("pubDate")))

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


def create_opml_from_files(
    rss_files: list[Path],
    save_path: Path,
    output_file="subscriptions.opml",
):
    """
    Create an OPML file from a list of RSS feed files or XML content strings.

    Args:
        rss_files_or_strings (list): List of file paths or raw RSS XML strings
        save_path (str): Path to save the OPML file
        output_file (str): Output OPML file name (default: "subscriptions.opml")

    Raises:
        Exception: If there is an error while creating the OPML file.

    Input Example:
        rss_files_or_strings = [
             "rss_feed1.xml",  # File path example
            \"\"\"<?xml version="1.0"?>
            <rss version="2.0">
                <channel>
                    <title>Example Feed</title>
                    <link>https://example.com/rss</link>
                </channel>
            </rss>\"\"\"  # Raw XML content
        ]
    """
    opml = ET.Element("opml", version="1.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "RSS Subscriptions"

    # Add date elements
    # RFC 2822 Format (Used in many OPML examples)
    now = datetime.datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")
    ET.SubElement(head, "dateCreated").text = now
    ET.SubElement(head, "dateModified").text = now

    body = ET.SubElement(opml, "body")

    for rss_file in rss_files:
        try:
            # Set the RSS feed link
            xml_url = add_base_url(str(f"{rss_file.name}"))

            # Parse XML from file
            tree = ET.parse(rss_file)
            root = tree.getroot()

            # Find the feed title
            title = None
            for elem in root.findall(".//channel/title"):
                title = elem.text
                break

            # Extract RSS feed URL (full or relative)
            html_url = None
            for elem in root.findall(".//channel/link"):
                html_url = elem.text
                break

            if not title:
                title = "Unknown Feed"

            # Create OPML outline entry
            ET.SubElement(
                body,
                "outline",
                type="rss",
                text=title,
                title=title,
                xmlUrl=xml_url,
                htmlUrl=html_url,
            )

        except Exception as e:
            print(f"Error processing RSS input: {e}")

    # create "feed" folder if not exists
    output_dir = Path(save_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_filename = Path(output_file)
    save_path = output_dir / xml_filename

    # Convert to string and save
    tree = ET.ElementTree(opml)
    tree.write(save_path, encoding="utf-8", xml_declaration=True)
    logging.info(f"OPML Saved RSS feed to file: {save_path}")


def main():
    """Entry point of the email to RSS feed converter.
    Reads email messages from the database, generates an RSS feed for each sender,
    and saves the feed to a file.
    """

    data_dir = Path(config.get("data_dir"))
    data_feed_dir = data_dir / "feed"

    rss_files = []

    for sender in db.get_senders():
        messages = db.get_email(sender)
        logging.info(f"{sender} found entries={messages.count()}")
        try:
            rss_feed = generate_rss(sender, messages)
            feed_file_path = save_feed(sender, rss_feed, save_path=data_feed_dir)
            rss_files.append(feed_file_path)
        except Exception as e:
            logging.error(f"Skipping {sender} due to error: {e}")
            continue

    # aggregate all the feeds into a single OPML file
    create_opml_from_files(rss_files, save_path=data_feed_dir)


# Main Execution
if __name__ == "__main__":
    main()
