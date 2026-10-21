"""``ingest news`` against two recorded Economic Times RSS feeds, asserted through the query layer.

The fixtures (see ``fixtures/ingest_news/README.md``) are the ET Markets and
ET Stocks feeds of 2026-10-21, five items each. Three stories appear in both
feeds with the same title and link and must be stored once. The instrument
master comes from the end-of-day fixtures (BAFNAPH, BAJAJ-AUTO, INFY,
RELIANCE, TCS), so an item naming "Reliance Industries", "Infosys" or "TCS"
is matched, and one naming only "Reliance Jio" or "Bajaj Finance" is not.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.news import Feed, NewsIngestResult, content_hash, ingest_news
from tradeline.transport import PlaybackTransport, Request, Response

FIXTURES = Path(__file__).parent / "fixtures" / "ingest_news"
MASTER_FIXTURES = Path(__file__).parent / "fixtures" / "ingest_eod_resend"
FEEDS = (
    Feed("et_markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    Feed("et_stocks", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
)
ITEMS_SEEN, SHARED = 10, 3
IST = ZoneInfo("Asia/Kolkata")
DAY_START = datetime(2026, 10, 21, tzinfo=IST)
DAY_END = datetime(2026, 10, 21, 23, 59, 59, tzinfo=IST)


def ist(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 10, 21, hour, minute, second, tzinfo=IST)


@dataclass(frozen=True)
class TwoRuns:
    first: NewsIngestResult
    second: NewsIngestResult


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> TwoRuns:
    """The instrument master, then both feeds ingested twice."""
    with app_connection(database_url) as conn:
        day = date(2024, 7, 1)
        ingest_eod(conn, PlaybackTransport(MASTER_FIXTURES), start=day, end=day)
        first = ingest_news(conn, PlaybackTransport(FIXTURES), feeds=FEEDS)
        second = ingest_news(conn, PlaybackTransport(FIXTURES), feeds=FEEDS)
    return TwoRuns(first, second)


@pytest.fixture
def conn(database_url: str, two_runs: TwoRuns) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_items_from_two_feeds_are_loaded_and_a_story_in_both_is_stored_once(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    items = queries.news(conn, None, start=DAY_START, end=DAY_END, as_of=DAY_END)
    run = queries.ingest_runs(conn, source="rss")[-1]  # oldest

    assert (two_runs.first.rows_seen, two_runs.first.rows_written) == (
        ITEMS_SEEN,
        ITEMS_SEEN - SHARED,
    )
    assert [(item.published_at, item.feed, item.title) for item in items] == [
        (
            ist(8, 25, 37),
            "et_stocks",
            "Bajaj Finance Share Price Live Updates: Bajaj Finance Faces Price Decline",
        ),
        (ist(9, 5, 20), "et_stocks", "TCS Share Price Live Updates: TCS Stock Details"),
        (
            ist(10, 47, 4),
            "et_markets",
            "Bajaj Finance shares fall 3% after Q2 results."
            " What Jefferies, Nomura, other brokerages now expect?",
        ),
        (
            ist(11, 0, 2),
            "et_markets",
            "Missed reversal in IT stocks? Jefferies gives 3 ideas with up to 50%"
            " re-rating potential",
        ),
        (
            ist(12, 58, 48),
            "et_markets",
            "Reliance Industries shares jump 3% as Jio’s Rs 30,213 crore IPO gets fully"
            " subscribed on Day 1. What lies ahead?",
        ),
        (
            ist(13, 32, 27),
            "et_markets",
            "Carlsberg said to near public filing for $700 million India IPO",
        ),
        (
            ist(13, 35, 7),
            "et_markets",
            "Jio IPO: Nithin Kamath explains how Jio played an important role in making"
            " what Zerodha is today",
        ),
    ]
    assert run.command == "ingest"
    assert (run.rows_seen, run.rows_written, run.error) == (ITEMS_SEEN, ITEMS_SEEN - SHARED, None)
    assert run.details["feeds"] == {"et_markets": 5, "et_stocks": 5}


def test_an_item_naming_a_symbol_or_company_exactly_is_matched_and_a_vague_name_is_not(
    conn: psycopg.Connection,
) -> None:
    def titles(symbol: str | None) -> list[str]:
        items = queries.news(conn, symbol, start=DAY_START, end=DAY_END, as_of=DAY_END)
        return [item.title[:40] for item in items]

    everything = queries.news(conn, None, start=DAY_START, end=DAY_END, as_of=DAY_END)
    unmatched = [item.title[:40] for item in everything if not item.symbols]

    # "TCS" as NSE writes it, and "Infosys and TCS" in the summary.
    assert titles("TCS") == [
        "TCS Share Price Live Updates: TCS Stock ",
        "Missed reversal in IT stocks? Jefferies ",
    ]
    assert titles("INFY") == ["Missed reversal in IT stocks? Jefferies "]
    # "Reliance Industries" is the master's "Reliance Industries Limited" without the suffix.
    assert titles("RELIANCE") == ["Reliance Industries shares jump 3% as Ji"]
    # "Reliance Jio" and "Bajaj Finance" name no company in the master exactly.
    assert titles("BAJAJ-AUTO") == []
    assert unmatched == [
        "Bajaj Finance Share Price Live Updates: ",
        "Bajaj Finance shares fall 3% after Q2 re",
        "Carlsberg said to near public filing for",
        "Jio IPO: Nithin Kamath explains how Jio ",
    ]


def test_a_second_run_over_the_same_feeds_inserts_nothing(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    newest = queries.ingest_runs(conn, source="rss")[0]
    items = queries.news(conn, None, start=DAY_START, end=DAY_END, as_of=DAY_END)

    assert (two_runs.second.rows_seen, two_runs.second.rows_written) == (ITEMS_SEEN, 0)
    assert (newest.rows_seen, newest.rows_written, newest.error) == (ITEMS_SEEN, 0, None)
    assert len(items) == ITEMS_SEEN - SHARED


def test_the_query_layer_returns_only_items_published_by_the_as_of_time(
    conn: psycopg.Connection,
) -> None:
    def titles(symbol: str, as_of: datetime) -> list[str]:
        items = queries.news(conn, symbol, start=DAY_START, end=DAY_END, as_of=as_of)
        return [item.title[:30] for item in items]

    reliance_published = ist(12, 58, 48)

    assert titles("RELIANCE", reliance_published.replace(second=47)) == []
    assert titles("RELIANCE", reliance_published) == ["Reliance Industries shares jum"]
    assert titles("TCS", ist(10, 0, 0)) == ["TCS Share Price Live Updates: "]
    assert titles("TCS", ist(11, 0, 2)) == [
        "TCS Share Price Live Updates: ",
        "Missed reversal in IT stocks? ",
    ]


def test_the_query_layer_returns_only_items_inside_the_range(conn: psycopg.Connection) -> None:
    items = queries.news(conn, "TCS", start=ist(9, 5, 21), end=ist(11, 0, 2), as_of=DAY_END)

    assert [item.title[:30] for item in items] == ["Missed reversal in IT stocks? "]


def test_the_query_layer_refuses_a_time_without_a_zone(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="as_of"):
        queries.news(conn, None, start=DAY_START, end=DAY_END, as_of=datetime(2026, 10, 21, 12))


class OneFeedDown:
    """Recorded fixtures, except that the ET Markets feed answers 503."""

    def __init__(self) -> None:
        self._playback = PlaybackTransport(FIXTURES)

    def send(self, request: Request) -> Response:
        if request.url == FEEDS[0].url:
            return Response(status=503, headers=(), body=b"")
        return self._playback.send(request)


def test_a_failing_feed_is_recorded_and_the_other_feeds_still_load(database_url: str) -> None:
    with app_connection(database_url) as conn:
        result = ingest_news(conn, OneFeedDown(), feeds=FEEDS)
        run = queries.ingest_runs(conn, source="rss")[0]

    assert result.failed_feeds == ("et_markets",)
    assert (result.rows_seen, run.rows_seen) == (5, 5)
    assert run.details["failed_feeds"] == ["et_markets"]
    assert run.details["feeds"] == {"et_stocks": 5}


def test_a_story_is_identified_by_its_title_and_link_up_to_case_spacing_and_tracking() -> None:
    title = "Reliance Industries shares jump 3%"
    link = "https://economictimes.indiatimes.com/markets/stocks/news/x/articleshow/135085466.cms"
    same = content_hash(title, link)

    assert content_hash("  reliance industries  SHARES jump 3% ", link) == same
    assert (
        content_hash(title, link.replace("https://economictimes", "HTTPS://EconomicTimes")) == same
    )
    assert content_hash(title, f"{link}?utm_source=rss&utm_medium=feed#top") == same
    assert content_hash(title, f"{link}?page=2") != same
    assert content_hash(f"{title} today", link) != same
