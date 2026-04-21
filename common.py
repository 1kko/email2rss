"""
Common functions and variables used by multiple scripts.
"""

import os
import logging
import secrets
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
    "img_proxy_secret": None,  # populated lazily by get_img_proxy_secret()
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
    try:
        fd = os.open(
            str(secret_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        # Another worker won the race — read what they wrote.
        return secret_path.read_bytes()
    try:
        os.write(fd, generated)
    finally:
        os.close(fd)
    return generated


def validate_reader_config() -> None:
    """Raise RuntimeError if the reader is enabled but config is incomplete."""
    if not config.get("enable_internal_reader"):
        return
    baseurl = config.get("server_baseurl")
    if not baseurl:
        raise RuntimeError(
            "enable_internal_reader=true requires server_baseurl to be set "
            "(the proxy origin for signed /img URLs). Set server_baseurl in .env."
        )
    # Defensive: baseurl flows into the iframe CSP meta tag. Reject anything
    # containing characters that could break out of the directive.
    if any(ch in baseurl for ch in (";", " ", "\t", "\n", "\r", '"', "'", "<", ">")):
        raise RuntimeError(
            f"server_baseurl {baseurl!r} contains illegal characters; "
            "expected a plain origin like 'http://localhost:8000' or 'https://example.com'."
        )
    if not (baseurl.startswith("http://") or baseurl.startswith("https://")):
        raise RuntimeError(
            f"server_baseurl {baseurl!r} must start with http:// or https://."
        )


def get_img_proxy_secret() -> bytes:
    """Cache-on-demand accessor for the HMAC secret."""
    if config["img_proxy_secret"] is None:
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
