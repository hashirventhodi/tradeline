"""``ingest news``: the configured RSS feeds into ``news_items``.

One ingest run (source ``rss``) fetches every feed once. An RSS feed holds only
its latest items, so the run has no date range: it stores whatever the feeds
carry now that is not already stored.

A story is identified by a content hash of its normalised title and link
(``content_hash``), so the same story in two feeds, or in the same feed on the
next run, is stored once. The title is normalised for Unicode form, case, and
whitespace; the link for scheme and host case, a trailing slash, the fragment,
and ``utm_*`` tracking parameters. A story whose title the publisher later
edits is a new story by this rule.

A feed that fails (an HTTP error or a body that is not RSS) is logged and
listed in the run's details, and the other feeds are still loaded, so one
publisher's outage does not cost the window for the rest. When every feed
fails, the run fails.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg

from tradeline.ingest import COMMAND, IngestRunCounters, record_ingest_run
from tradeline.sources import rss
from tradeline.text_matching import TextMatcher
from tradeline.transport import Transport

log = logging.getLogger(__name__)

SOURCE = "rss"


@dataclass(frozen=True)
class Feed:
    name: str
    url: str


@dataclass(frozen=True)
class NewsIngestResult:
    rows_seen: int
    rows_written: int
    failed_feeds: tuple[str, ...]


def ingest_news(
    conn: psycopg.Connection, transport: Transport, *, feeds: Sequence[Feed]
) -> NewsIngestResult:
    """Fetch every feed once and store the stories not already stored."""
    if not feeds:
        raise ValueError("no news feeds are configured ([news.feeds] in tradeline.toml)")
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        _load(conn, transport, run, feeds)
    return NewsIngestResult(
        rows_seen=run.rows_seen,
        rows_written=run.rows_written,
        failed_feeds=tuple(run.details["failed_feeds"]),
    )


@dataclass(frozen=True)
class _Row:
    content_hash: str
    feed: str
    item: rss.FeedItem
    symbols: tuple[str, ...]


def _load(
    conn: psycopg.Connection,
    transport: Transport,
    run: IngestRunCounters,
    feeds: Sequence[Feed],
) -> None:
    per_feed: dict[str, int] = {}
    failed: list[str] = []
    run.details = {"feeds": per_feed, "failed_feeds": failed}
    fetched_at = datetime.now(UTC)
    matcher = TextMatcher.load(conn)
    for feed in feeds:
        try:
            items = rss.fetch_feed(transport, feed.url)
        except rss.RssError as error:
            log.error(
                "feed failed", extra={"feed": feed.name, "url": feed.url, "error": str(error)}
            )
            failed.append(feed.name)
            continue
        rows = [_row(feed, item, matcher) for item in items]
        written = _insert(conn, rows, fetched_at)
        conn.commit()
        per_feed[feed.name] = len(items)
        run.rows_seen += len(items)
        run.rows_written += written
        log.info(
            "loaded feed", extra={"feed": feed.name, "rows": len(items), "rows_written": written}
        )
    if len(failed) == len(feeds):
        raise rss.RssError(f"every feed failed: {', '.join(failed)}")


def _row(feed: Feed, item: rss.FeedItem, matcher: TextMatcher) -> _Row:
    text = item.title if item.summary is None else f"{item.title}\n{item.summary}"
    return _Row(content_hash(item.title, item.link), feed.name, item, matcher.match(text))


def content_hash(title: str, link: str) -> str:
    """The identity of a story: SHA-256 of its normalised title and link."""
    text = f"{_normalise_title(title)}\n{_normalise_link(link)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def _normalise_link(link: str) -> str:
    parts = urlsplit(link.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _insert(conn: psycopg.Connection, rows: list[_Row], fetched_at: datetime) -> int:
    # A feed occasionally lists one story twice; the first copy is the one offered.
    unique: dict[str, _Row] = {}
    for row in rows:
        unique.setdefault(row.content_hash, row)
    if not unique:
        return 0
    values = list(unique.values())
    result = conn.execute(
        "WITH inserted AS ("
        "  INSERT INTO news_items"
        "   (content_hash, feed, title, link, summary, published_at, fetched_at, symbols)"
        "  SELECT n.content_hash, n.feed, n.title, n.link, n.summary, n.published_at, %s,"
        "   string_to_array(n.symbols, ' ')"
        "  FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[],"
        "   %s::timestamptz[], %s::text[])"
        "   AS n(content_hash, feed, title, link, summary, published_at, symbols)"
        "  ON CONFLICT (content_hash) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        (
            fetched_at,
            [r.content_hash for r in values],
            [r.feed for r in values],
            [r.item.title for r in values],
            [r.item.link for r in values],
            [r.item.summary for r in values],
            [r.item.published_at for r in values],
            [" ".join(r.symbols) for r in values],
        ),
    ).fetchone()
    assert result is not None
    return int(result[0])
