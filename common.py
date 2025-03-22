"""
Common functions and variables used by multiple scripts.
"""

import os
import logging
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv

load_dotenv()

config = {
    "imap_server": os.getenv("imap_server"),
    "userid": os.getenv("userid"),
    "userpw": os.getenv("userpw"),
    "mailbox": os.getenv("mailbox", "INBOX"),
    "port": int(os.getenv("port", "8000")),
    "refresh_seconds": int(os.getenv("refresh_seconds", "300")),
    "data_dir": os.getenv("data_dir", "data"),
    "max_item_per_feed": int(os.getenv("max_item_per_feed", "100")),
    "server_baseurl": os.getenv("server_baseurl"),
}


# Set up logging
logging_path = os.path.join(config.get("data_dir"), "email2rss.log")

# Create a timed rotating file handler that rotates the log file every week
file_handler = TimedRotatingFileHandler(
    logging_path, when="D", interval=1, backupCount=15
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
    handlers=[logging.FileHandler(logging_path), logging.StreamHandler()],
)
