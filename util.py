"""
This is a utility module that contains functions for extracting email addresses from strings.

"""

import re
import email
import email.header


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
        email_address = match.group(0).lower()
    else:
        email_address = default
    return email_address


def extract_name_from_email(email_address: str) -> str:
    """
    Extracts the name from an email address.

    Args:
        email_address (str): The email address.

    Returns:
        str: The name extracted from the email address,
             or email address itself if no name is found.
    """
    name, addr = email.utils.parseaddr(email_address)
    if name:
        return name
    return addr


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


def utf8_decoder(data: bytes):
    """
    Decodes a list of byte strings using UTF-8 encoding.

    Args:
        data (bytes): A list of byte strings to be decoded.

    Returns:
        str: The decoded string.

    """
    dec_data = email.header.decode_header(data)
    return str(
        "".join(
            [
                (
                    str(title, encoding or "utf-8")
                    if isinstance(title, bytes)
                    else str(title)
                )
                for title, encoding in dec_data
            ]
        )
    )


def cleanse_content(content):
    """Remove non-XML compatible characters from content using regular expressions.
    This function removes control characters and NULL bytes, except for tab (ASCII 9),
    line feed (ASCII 10), and carriage return (ASCII 13), which are valid in XML.
    """
    # Regex to match invalid XML characters
    # This pattern excludes ASCII values 9 (tab), 10 (newline), and 13 (carriage return), which are acceptable in XML.
    invalid_xml_chars = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
    return invalid_xml_chars.sub("", content)
