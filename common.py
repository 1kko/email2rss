"""
Common functions and variables used by multiple scripts.
"""

import os
import logging
import secrets
import stat
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path as _Path

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
    "enable_internal_reader": os.getenv("enable_internal_reader", "false").lower() == "true",
    "bind_address": os.getenv("bind_address", "127.0.0.1"),
}


def _load_or_create_img_proxy_secret() -> bytes:
    """
    Return the HMAC secret bytes for /img URL signing.

    Precedence:
        1. env var `img_proxy_secret` (any non-empty string) — used as-is, UTF-8 encoded
        2. existing file `{data_dir}/img_proxy_secret` — reused across restarts
        3. newly generated 32-byte urlsafe random secret — persisted to the file with mode 0600
    """
    env_val = os.getenv("img_proxy_secret")
    if env_val:
        return env_val.encode("utf-8")

    secret_path = _Path(config["data_dir"]) / "img_proxy_secret"
    if secret_path.exists():
        return secret_path.read_bytes()

    secret_path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32).encode("ascii")
    secret_path.write_bytes(generated)
    secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return generated


def validate_reader_config() -> None:
    """Raise RuntimeError if the reader is enabled but config is incomplete."""
    if config.get("enable_internal_reader") and not config.get("server_baseurl"):
        raise RuntimeError(
            "enable_internal_reader=true requires server_baseurl to be set "
            "(the proxy origin for signed /img URLs). Set server_baseurl in .env."
        )


# Secret is loaded lazily — on first access via config["img_proxy_secret"]
# to avoid side effects at module import. Tests that need the secret call
# _load_or_create_img_proxy_secret() directly or access config["img_proxy_secret"].
config["img_proxy_secret"] = None  # populated by get_img_proxy_secret()


def get_img_proxy_secret() -> bytes:
    """Cache-on-demand accessor for the HMAC secret."""
    if not config["img_proxy_secret"]:
        config["img_proxy_secret"] = _load_or_create_img_proxy_secret()
    return config["img_proxy_secret"]


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
