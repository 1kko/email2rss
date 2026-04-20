"""Tests for feed_generator.generate_rss — XML structure and link-mode switching."""
import datetime

from defusedxml.ElementTree import fromstring as safe_fromstring

import feed_generator
from tests.conftest import insert_email


def _rss_items(xml_str: str):
    root = safe_fromstring(xml_str)
    return root.findall(".//channel/item")


def test_generate_rss_produces_parseable_rss_xml(db_session):
    insert_email(db_session, email_id=1, subject="First")
    insert_email(db_session, email_id=2, subject="Second", timestamp=datetime.datetime(2026, 4, 14))

    messages = list(db_session.query(feed_generator.db.Email).filter_by(sender="sender@example.com"))
    xml = feed_generator.generate_rss("sender@example.com", messages)
    items = _rss_items(xml)
    assert len(items) == 2
    titles = [it.findtext("title") for it in items]
    assert {"First", "Second"}.issubset(set(titles))


def test_generate_rss_reverses_input_order(db_session):
    """Characterize actual RSS item ordering.

    DB returns newest-first: [Newest, Middle, Oldest].
    generate_rss reverses to [Oldest, Middle, Newest] before iterating, but
    feedgen.add_entry() prepends each entry (newest index 0), so the reversal
    and the prepend cancel out: RSS output order is newest-first,
    matching the original DB query order.
    """
    insert_email(db_session, email_id=1, subject="Oldest", timestamp=datetime.datetime(2026, 4, 10))
    insert_email(db_session, email_id=2, subject="Middle", timestamp=datetime.datetime(2026, 4, 11))
    insert_email(db_session, email_id=3, subject="Newest", timestamp=datetime.datetime(2026, 4, 12))

    # DB returns newest-first: [Newest, Middle, Oldest]
    # generate_rss reverses → iterates [Oldest, Middle, Newest]
    # feedgen prepends each → RSS XML: [Newest, Middle, Oldest]
    messages = list(feed_generator.db.get_email("sender@example.com"))
    xml = feed_generator.generate_rss("sender@example.com", messages)
    titles = [it.findtext("title") for it in _rss_items(xml)]
    assert titles == ["Newest", "Middle", "Oldest"]


def test_internal_reader_mode_links_to_article_viewer(db_session, monkeypatch):
    monkeypatch.setitem(feed_generator.config, "enable_internal_reader", True)
    monkeypatch.setitem(feed_generator.config, "server_baseurl", "http://testserver")
    insert_email(db_session, email_id=1)

    messages = list(feed_generator.db.get_email("sender@example.com"))
    xml = feed_generator.generate_rss("sender@example.com", messages)
    link = _rss_items(xml)[0].findtext("link")
    assert link.startswith("http://testserver/article/sender_example_com/")


def test_external_mode_links_to_sender_domain(db_session, monkeypatch):
    monkeypatch.setitem(feed_generator.config, "enable_internal_reader", False)
    insert_email(db_session, email_id=1, sender="hello@tailscale.com")

    messages = list(feed_generator.db.get_email("hello@tailscale.com"))
    xml = feed_generator.generate_rss("hello@tailscale.com", messages)
    link = _rss_items(xml)[0].findtext("link")
    assert link == "https://tailscale.com"
