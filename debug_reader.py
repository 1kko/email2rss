#!/usr/bin/env python3
"""
Debug script for internal RSS reader troubleshooting
"""
import sys
import email
import hashlib
from common import config
import database as db

def debug_info():
    print("=" * 60)
    print("Internal RSS Reader Debug Information")
    print("=" * 60)

    # Check configuration
    print("\n[1] Configuration Check:")
    print(f"  enable_internal_reader: {config.get('enable_internal_reader')}")
    print(f"  data_dir: {config.get('data_dir')}")
    print(f"  server_baseurl: {config.get('server_baseurl')}")

    # Check database
    print("\n[2] Database Check:")
    entry_count = db.get_entry_count()
    print(f"  Total emails in database: {entry_count}")

    if entry_count == 0:
        print("  ⚠️  Database is empty! Fetch some emails first.")
        return

    # List all senders
    print("\n[3] Senders in Database:")
    senders = db.get_senders()
    for idx, sender in enumerate(senders, 1):
        emails = db.get_email(sender)
        print(f"  {idx}. {sender} ({emails.count()} emails)")

    # Check specific sender if provided
    if len(sys.argv) > 1:
        target_sender = sys.argv[1]
        print(f"\n[4] Checking emails from: {target_sender}")

        emails = db.get_email(target_sender)
        email_count = emails.count()

        if email_count == 0:
            print(f"  ⚠️  No emails found from {target_sender}")
            print(f"  Available senders: {', '.join(senders)}")
            return

        print(f"  Found {email_count} emails")
        print("\n[5] Email Details with GUIDs:")

        for idx, email_record in enumerate(emails, 1):
            try:
                msg = email.message_from_bytes(email_record.content)

                # Calculate GUID
                unique_string = msg["subject"] + msg["date"] + msg["from"]
                guid = hashlib.md5(unique_string.encode()).hexdigest()

                # Extract subject
                subject = email.header.make_header(email.header.decode_header(msg["subject"]))

                # Generate sanitized feed name
                email_addr = target_sender
                sanitized = email_addr.replace("@", "_").replace(".", "_")

                # Generate internal link
                internal_link = f"/article/{sanitized}/{guid}"

                print(f"\n  Email #{idx}:")
                print(f"    Subject: {subject}")
                print(f"    Date: {msg['date']}")
                print(f"    GUID: {guid}")
                print(f"    Internal URL: {internal_link}")

            except Exception as e:
                print(f"  ⚠️  Error processing email #{idx}: {e}")

    # Test GUID lookup if provided
    if len(sys.argv) > 2:
        target_guid = sys.argv[2]
        print(f"\n[6] Testing GUID Lookup: {target_guid}")

        target_sender = sys.argv[1]
        email_record = db.get_email_by_guid(target_sender, target_guid)

        if email_record:
            print(f"  ✅ Email found!")
            msg = email.message_from_bytes(email_record.content)
            subject = email.header.make_header(email.header.decode_header(msg["subject"]))
            print(f"    Subject: {subject}")
            print(f"    Sender: {email_record.sender}")
            print(f"    Timestamp: {email_record.timestamp}")
        else:
            print(f"  ❌ No email found with GUID: {target_guid}")
            print(f"  Sender searched: {target_sender}")

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("Usage:")
        print("  python debug_reader.py                    # Show all senders")
        print("  python debug_reader.py <sender_email>     # Show emails and GUIDs for sender")
        print("  python debug_reader.py <sender> <guid>    # Test GUID lookup")
        print("\nRunning basic check...\n")

    debug_info()
