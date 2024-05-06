"""
Common functions and variables used by multiple scripts.
"""

import logging
from dotenv import dotenv_values
import os

config = {
    "user_email": os.getenv("user_email"),
    "app_password": os.getenv("app_password"),
    "DIRECTORY": os.getenv("DIRECTORY", "rss_feed"),
    "PORT": os.getenv("PORT", 8000),
}
# dotenv_values(".env")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s - %(filename)s:%(lineno)d",
    handlers=[logging.FileHandler("email_to_rss.log"), logging.StreamHandler()],
)
