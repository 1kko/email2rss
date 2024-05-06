"""
Common functions and variables used by multiple scripts.
"""

import logging
from dotenv import dotenv_values
import os

config = {
    "imap_server": os.getenv("imap_server"),
    "userid": os.getenv("userid"),
    "userpw": os.getenv("userpw"),
    "mailbox": os.getenv("mailbox", "INBOX"),
    "PORT": os.getenv("PORT", "8000"),
}
# dotenv_values(".env")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s - %(filename)s:%(lineno)d",
    handlers=[logging.FileHandler("email_to_rss.log"), logging.StreamHandler()],
)
