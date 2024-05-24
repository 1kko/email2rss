#!/usr/bin/env python3

"""Retrieve emails from a Gmail account and convert them to an RSS feed.

This script connects to a Gmail account, fetches emails from a specific mailbox,
and saves to a database.

"""
from __future__ import annotations

import datetime

import email
import email.header
import imaplib

import database as db
from common import logging, config
from util import extract_email_address


def connect_to_gmail(imap_server, username, password, mailbox="INBOX"):
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
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(username, password)
        mail.select(mailbox)
        logging.info(f"Connected to Email and selected {mailbox}.")
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

    # TODO: need to fetch from database for the latest timestamp of database if since is None.
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
            receiver = extract_email_address(msg["to"], default="you@email.com")
            logging.info(
                f"Email from {sender}. title: {msg['subject']} by {msg['date']}"
            )
            article_date = email.utils.parsedate_to_datetime(msg["date"])

            # save to database
            db.save_email(
                sender=sender,
                receiver=receiver,
                subject=msg["subject"],
                email_id=int(num),
                content=data[0][1],
                timestamp=article_date,
            )

            if sender not in emails:
                emails[sender] = []
            emails[sender].append(msg)
        logging.info("Fetched emails and grouped by sender.")
        return emails
    except Exception as e:
        logging.error(f"Failed to fetch or process emails: {e}")
        raise


def main():
    """
    Entry point of the email to RSS feed converter.

    This function connects to Gmail using the provided user email and app password,
    fetches emails from the last 24 hours

    Raises:
        Exception: If an error occurs during execution.

    Returns:
        None
    """
    imap_server = config.get("imap_server")
    userid = config.get("userid")
    userpw = config.get("userpw")
    mailbox = config.get("mailbox")

    # if emails.db does not exist since should be 30, otherwise 1
    # 30 to populate the rss feed for the first time
    since = 1
    if db.get_entry_count() == 0:
        since = 30

    try:
        service = connect_to_gmail(imap_server, userid, userpw, mailbox)
        before_id = db.get_last_email_id()
        _ = fetch_emails(service, since=since)
        after_id = db.get_last_email_id()

        # don't build if the email id is same.
        # if last email id is same, no need to build the rss feed.
        if before_id == after_id:
            logging.info("No new emails found. Skipping RSS feed generation.")
            return

    except Exception as e:
        logging.error(f"An error occurred during execution: {e}")


# Main Execution
if __name__ == "__main__":
    main()
