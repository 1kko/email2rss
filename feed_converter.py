#!/usr/bin/env python3

"""Retrieve emails from a Gmail account and convert them to an RSS feed.

This script connects to a Gmail account, fetches emails from a specific mailbox,
and converts them into an RSS feed. The RSS feed is then saved to a file.

Author: 1kko

"""
from __future__ import annotations

import datetime
import imaplib
import email
from common import logging, config
import re
from pathlib import Path

from feedgen.feed import FeedGenerator


def extract_email_address(email_address: str, default: str | None = None) -> str:
    """
    Extracts the email address from a given string.

    Args:
        email_address (str): The input string containing an email address.
        default (str | None, optional): The default value to return
                                if no email address is found. Defaults to None.

    Returns:
        str: The extracted email address.

    """
    match = re.search(r"[\w\.-]+@[\w\.-]+", email_address)
    if match:
        email_address = match.group(0)
    else:
        email_address = default
    return email_address


def extract_domain_address(email_address: str, default=None) -> str:
    """
    Extracts the domain address from an email.

    Args:
        email_address (str): The email address.
        default (Any, optional): The default value to return
                                 if no domain is found. Defaults to None.

    Returns:
        str: The domain address extracted from the email,
             or the default value if no domain is found.
    """
    match = re.search(r"@([\w\.-]+)", email_address)
    if match:
        domain = match.group(1)
    else:
        domain = default
    return domain


def connect_to_gmail(username, password):
    """
    Connects to Gmail using the provided username and password.

    Args:
        username (str): The Gmail username.
        password (str): The Gmail password.

    Returns:
        imaplib.IMAP4_SSL: The connected IMAP4_SSL object.

    Raises:
        Exception: If there is an error connecting to Gmail.

    """
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(username, password)
        mail.select("Meco_911265aa-0cec-454a-a50d-7361719203d6")
        logging.info("Connected to Gmail and selected newsletters.")
        return mail
    except Exception as e:
        logging.error(f"Failed to connect to Gmail: {e}")
        raise


def fetch_emails(mail, since=10):
    """
    Fetches emails from a given mailbox since a specified number of days ago.

    Args:
        mail (object): The mailbox object used to fetch emails.
        since (int, optional): The number of days ago to start fetching emails from. Defaults to 10.

    Returns:
        dict: A dictionary containing emails grouped by sender.

    Raises:
        Exception: If there is an error while fetching or processing emails.
    """
    logging.info("Fetching emails")
    last_n_day = (datetime.date.today() - datetime.timedelta(days=since)).strftime(
        "%d-%b-%Y"
    )
    try:
        _, msg = mail.search(None, f"(SINCE {last_n_day})")
        messages = msg[0].split()
        logging.info(f"Found {len(messages)} emails in the last {since} days.")
        emails = {}
        for index, num in enumerate(messages):
            logging.info(f"Processing email {index + 1} of {len(messages)}.")
            _, data = mail.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            sender = extract_email_address(msg["from"], default="unknown@email.com")
            logging.info(
                f"Email from {sender}. title: {msg['subject']} by {msg['date']}"
            )
            if sender not in emails:
                emails[sender] = []
            emails[sender].append(msg)
        logging.info("Fetched emails and grouped by sender.")
        return emails
    except Exception as e:
        logging.error(f"Failed to fetch or process emails: {e}")
        raise


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
        fg = FeedGenerator()
        fg.title(f"{sender}")
        fg.link(href="http://#", rel="alternate")
        fg.description(f"RSS feed for emails from {sender}")

        for msg in messages:
            fe = fg.add_entry()
            fe.title(msg["subject"] or "No Subject")

            fe.link(href=f"https://{extract_domain_address(sender)}")

            # Assuming the email payload might be in different parts or encoded
            if msg.is_multipart():
                content = ""
                for part in msg.walk():
                    ctype = part.get_content_type()
                    cdispo = str(part.get("Content-Disposition"))

                    # Skip any text/plain (txt) attachments
                    if ctype == "text/plain" and "attachment" not in cdispo:
                        charset = part.get_content_charset()
                        if charset is not None:
                            content += part.get_payload(decode=True).decode(charset)
                        else:
                            content += part.get_payload()
                fe.description(content)
            else:
                # Non-multipart emails are simpler, just get the payload directly
                charset = msg.get_content_charset()
                if charset is not None:
                    fe.description(msg.get_payload(decode=True).decode(charset))
                else:
                    fe.description(msg.get_payload())

        logging.info(f"Generated RSS feed for {sender}.")
        return fg.rss_str(pretty=True).decode("utf-8")
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
        email_address = extract_email_address(sender, default="unknown_email")

        # Sanitize the email address to be safe for use as a filename
        sanitized_email = email_address.replace("@", "_").replace(".", "_")
        # create "rss_feed" folder if not exists
        output_dir = Path(save_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        xml_filename = Path(f"{sanitized_email}.xml")
        save_path = output_dir / xml_filename

        with open(save_path, "w") as f:
            f.write(feed_content)
        logging.info(f"Saved RSS feed to file {save_path}.")
        return save_path
    except Exception as e:
        logging.error(f"Failed to save RSS feed for {sender}: {e}")
        raise


def main():
    user_email = config.get("user_email")
    app_password = config.get("app_password")

    try:
        service = connect_to_gmail(user_email, app_password)
        emails_by_sender = fetch_emails(service, since=1)
        DIRECTORY = config.get("directory", "rss_feed")

        # need to sort the emails by sender and feed to generate_rss
        for sender, messages in emails_by_sender.items():
            rss_feed = generate_rss(sender, messages)
            filename = save_feed(sender, rss_feed, save_path=DIRECTORY)
    except Exception as e:
        logging.error(f"An error occurred during execution: {e}")


# Main Execution
if __name__ == "__main__":
    main()
