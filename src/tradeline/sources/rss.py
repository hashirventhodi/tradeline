"""RSS 2.0 news feeds.

One GET per feed URL returns the feed's current items; there is no history and
no paging, so the collector has to run often enough not to miss items that roll
off. Each ``<item>`` becomes a ``FeedItem`` with its title, link, description
(as the summary), and ``pubDate``. A ``pubDate`` that is missing or cannot be
read is ``None``; one without a zone (RFC 2822's ``-0000``) is taken as UTC.

Feeds are parsed with the standard library's expat-based parser, which refuses
external entities and limits entity expansion.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from tradeline.transport import Request, Transport

log = logging.getLogger(__name__)

# Some publishers refuse requests without a browser-like agent.
HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}


class RssError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    summary: str | None
    published_at: datetime | None


def fetch_feed(transport: Transport, url: str) -> list[FeedItem]:
    try:
        response = transport.send(Request("GET", url, headers=HEADERS))
    except httpx.TransportError as error:  # the live transport's connection failures
        raise RssError(f"feed {url} could not be fetched: {error}") from error
    if response.status != 200:
        raise RssError(f"feed {url} answered HTTP {response.status}")
    return parse_feed(response.body)


def parse_feed(body: bytes) -> list[FeedItem]:
    """Every item with a title and a link, in feed order; others are skipped."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise RssError(f"feed is not well-formed XML: {error}") from error
    if root.tag != "rss":
        raise RssError(f"not an RSS 2.0 feed: root element is <{root.tag}>")
    items: list[FeedItem] = []
    for element in root.iterfind("./channel/item"):
        title = _text(element.findtext("title"))
        link = _text(element.findtext("link"))
        if title is None or link is None:
            log.warning("feed item without a title or link skipped", extra={"title": title})
            continue
        items.append(
            FeedItem(
                title=title,
                link=link,
                summary=_text(element.findtext("description")),
                published_at=_published_at(element.findtext("pubDate")),
            )
        )
    return items


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = " ".join(value.split())
    return stripped or None


def _published_at(value: str | None) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        log.warning("unreadable pubDate", extra={"pub_date": text})
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
