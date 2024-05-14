"""
Database module for the application.
"""

import datetime

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, BLOB
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from common import config

Base = declarative_base()


class Email(Base):
    """
    Represents an email entity.

    Attributes:
        id (int): The unique identifier of the email.
        sender (str): The sender of the email.
        subject (str): The subject of the email.
        content (str): The content of the email.
        timestamp (datetime): The timestamp when the email was received.
    """

    __tablename__ = "emails"

    id = Column(Integer, primary_key=True)
    receiver = Column(String)
    sender = Column(String)
    email_id = Column(Integer)
    subject = Column(Text)
    content = Column(BLOB)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

data_dir = config.get("data_dir", "data")
engine = create_engine(f"sqlite:///{data_dir}/emails.db", poolclass=NullPool)
Base.metadata.create_all(engine)

Session = sessionmaker(bind=engine)


def save_email(
    sender: str,
    receiver: str,
    email_id: int,
    subject: str,
    content: bytes,
    timestamp: datetime,
):
    """
    Save an email to the database.

    Args:
        sender (str): The sender of the email.
        receiver (str): The receiver of the email.
        email_id (int): The unique identifier of the email.
        subject (str): The subject of the email.
        content (bytes): The content of the email.
        timestamp (datetime): The timestamp when the email was received.

    Returns:
        None
    """

    with Session.begin() as session:
        existing_email = session.query(Email).filter_by(email_id=email_id).first()
        if existing_email is None:
            email = Email(
                sender=sender,
                receiver=receiver,
                email_id=email_id,
                subject=subject,
                content=content,
                timestamp=timestamp,
            )
            session.add(email)
            session.commit()
        else:
            print(f"Email with id {email_id} already exists. Discarding.")


def get_email(sender: str) -> list:
    """
    Get all emails from a specific sender.

    Args:
        sender (str): The sender of the emails.

    Returns:
        list: A list of email objects.
    """

    max_item_per_feed = config.get("max_item_per_feed")

    with Session.begin() as session:
        emails = (
            session.query(Email)
            .filter_by(sender=sender)
            .order_by(Email.timestamp.desc())
            .limit(max_item_per_feed)
        )
        return emails


def get_senders() -> list:
    """
    Get all unique senders from the database.

    Returns:
        list: A list of unique sender email addresses.
    """

    with Session.begin() as session:
        senders = session.query(Email.sender).distinct().all()
        return [sender[0] for sender in senders]


def get_entry_count():
    """
    Check if the database is empty.

    Returns:
        bool: True if the database is empty, False otherwise.
    """

    with Session.begin() as session:
        return session.query(Email).count()


def get_last_email_id():
    """
    Get the last email id from the database.

    Returns:
        int: The last email id.
    """

    with Session.begin() as session:
        last_email = session.query(Email).order_by(Email.timestamp.desc()).first()
        if last_email:
            return last_email.email_id
        return 0
