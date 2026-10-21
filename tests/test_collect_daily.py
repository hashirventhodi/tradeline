"""``collect-daily``: one day's end-of-day load, Universe candles, announcements, and news.

The day is Thursday 1 August 2024, the first trading day of a month, so the
day's Universe is computed by the collection itself. Each source plays its
own real recorded responses back:

- ``fixtures/universe/``: the NSE end-of-day files for July and 1 August
  2024, the equity master, and Kite's instrument list, trimmed to eight
  symbols. July is loaded before the collection; 1 August by it.
- ``fixtures/ingest_candles/``: Kite's daily candles for 1 July to 30 August
  2024. Kite is asked for the one day; ``OneDayFromRecordedRange`` serves that
  request from the recorded two-month response, keeping only the day's bar.
- ``fixtures/collect_daily/``: NSE's and BSE's announcements of 1 August 2024,
  trimmed to the Universe's four companies (see its README).
- ``fixtures/ingest_news/``: two Economic Times RSS feeds, five items each,
  three of them in both.

The Universe Rule is shrunk as in ``test_universe.py``, with four places, so
August's Universe is RELIANCE, INFY, TCS, and BAJAJ-AUTO.

A failing source is injected at playback by ``FailingHost``, which cannot
connect to one host and plays the rest back, so no fixture is edited.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest

from tradeline import queries
from tradeline.cli import main
from tradeline.collect import CollectResult, collect_daily
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.news import Feed
from tradeline.secrets import MissingSecret
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport, Request, Response, UnrecordedRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
DAY = date(2024, 8, 1)
AUGUST = date(2024, 8, 1)
RULE = UniverseRule(
    top_n=4, lookback_trading_days=20, min_listing_trading_days=21, min_presence_percent=90
)
FEEDS = (
    Feed("et_markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    Feed("et_stocks", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
)
SOURCES = {
    "nse_equity_master",
    "nse_eod",
    "kite_instruments",
    "kite_candles_daily",
    "nse_announcements",
    "bse_announcements",
    "rss",
}


class Fixtures:
    """Plays back from several recorded directories, the first that has the request."""

    def __init__(self, *dirs: Path) -> None:
        self._playbacks = [PlaybackTransport(d) for d in dirs]

    def send(self, request: Request) -> Response:
        for playback in self._playbacks:
            try:
                return playback.send(request)
            except UnrecordedRequest:
                continue
        raise UnrecordedRequest(f"no fixture for {request.describe()} in any directory")


class OneDayFromRecordedRange:
    """Serves a Kite candle request for a range inside the recorded one, trimmed to that range."""

    RECORDED = ("2024-07-01", "2024-08-30")

    def __init__(self) -> None:
        self._inner = PlaybackTransport(FIXTURES / "ingest_candles")

    def send(self, request: Request) -> Response:
        parts = urlsplit(request.url)
        query = parse_qs(parts.query)
        start, end = query["from"][0], query["to"][0]
        recorded = parts._replace(query=f"from={self.RECORDED[0]}&to={self.RECORDED[1]}")
        response = self._inner.send(Request(request.method, recorded.geturl(), request.headers))
        body = json.loads(response.body)
        body["data"]["candles"] = [c for c in body["data"]["candles"] if start <= c[0][:10] <= end]
        return Response(response.status, response.headers, json.dumps(body).encode())


class FailingHost:
    """Cannot connect to ``host``, as when it is down; passes the rest to ``inner``.

    A connection error rather than a 5xx, which the adapters retry with real delays.
    """

    def __init__(self, inner: Fixtures, host: str) -> None:
        self._inner = inner
        self._host = host

    def send(self, request: Request) -> Response:
        if urlsplit(request.url).hostname == self._host:
            raise httpx.ConnectError(f"cannot connect to {self._host}")
        return self._inner.send(request)


def sources() -> Fixtures:
    return Fixtures(FIXTURES / "universe", FIXTURES / "collect_daily", FIXTURES / "ingest_news")


def kite() -> KiteSession:
    return KiteSession(
        OneDayFromRecordedRange(), api_key="key", access_token="tok", sleep=lambda _: None
    )


def collect(conn: psycopg.Connection, transport: Fixtures | FailingHost) -> CollectResult:
    return collect_daily(
        conn,
        day=DAY,
        rule=RULE,
        feeds=FEEDS,
        transport=transport,
        browser_transport=transport,
        kite=kite,
    )


@pytest.fixture(scope="module")
def july_loaded(database_url: str) -> None:
    """End-of-day files for July, so August's Universe can be computed."""
    with app_connection(database_url) as conn:
        ingest_eod(
            conn,
            PlaybackTransport(FIXTURES / "universe"),
            start=date(2024, 7, 1),
            end=date(2024, 7, 31),
        )


@pytest.fixture
def conn(database_url: str, july_loaded: None) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_a_collection_loads_every_source_for_the_day_and_records_one_run_each(
    conn: psycopg.Connection,
) -> None:
    result = collect(conn, sources())

    assert result.succeeded
    runs = queries.ingest_runs(conn, collection_id=result.collection_id)
    assert sorted(r.source for r in runs) == sorted(SOURCES)
    assert {r.status for r in runs} == {"succeeded"}
    by_source = {r.source: r for r in runs}
    assert by_source["nse_eod"].range_start == by_source["nse_eod"].range_end == DAY
    assert by_source["kite_candles_daily"].rows_seen == 4  # one bar per Universe symbol
    assert by_source["nse_announcements"].rows_seen == 5
    assert by_source["bse_announcements"].rows_seen == 5
    assert by_source["rss"].rows_seen == 10
    assert all(r.duration >= timedelta(0) for r in runs)

    universe = queries.universe_membership(conn, AUGUST, as_of=DAY, rule_version=RULE.version)
    assert [m.symbol for m in universe] == ["RELIANCE", "INFY", "TCS", "BAJAJ-AUTO"]
    (infy,) = queries.daily_candles(conn, "INFY", start=DAY, end=DAY)
    assert infy.close == Decimal("1813.4")
    assert queries.end_of_day(conn, "INFY", start=DAY, end=DAY, series="EQ")


def test_a_failing_source_is_recorded_as_failed_and_the_sources_after_it_still_run(
    conn: psycopg.Connection,
) -> None:
    result = collect(conn, FailingHost(sources(), "www.nseindia.com"))

    assert not result.succeeded
    assert result.failed_sources == ("nse_announcements",)
    runs = {r.source: r for r in queries.ingest_runs(conn, collection_id=result.collection_id)}
    assert sorted(runs) == sorted(SOURCES)
    failed = runs["nse_announcements"]
    assert failed.status == "failed"
    assert (
        failed.error is not None
        and failed.error == "ConnectError: cannot connect to www.nseindia.com"
    )
    assert runs["bse_announcements"].status == "succeeded"
    assert runs["bse_announcements"].rows_seen == 5
    assert runs["rss"].status == "succeeded"
    assert runs["rss"].rows_seen == 10


def test_sources_a_failed_step_never_reached_are_recorded_as_not_run(
    conn: psycopg.Connection,
) -> None:
    """NSE's archives are down, so the instrument master fails and ends the eod step."""
    result = collect(conn, FailingHost(sources(), "nsearchives.nseindia.com"))

    runs = {r.source: r for r in queries.ingest_runs(conn, collection_id=result.collection_id)}
    assert sorted(runs) == sorted(SOURCES)
    assert runs["nse_equity_master"].error == (
        "ConnectError: cannot connect to nsearchives.nseindia.com"
    )
    for source in ("nse_eod", "kite_instruments"):
        assert runs[source].status == "failed"
        assert runs[source].command == "collect-daily"
        assert runs[source].error == (
            "NotRun: not run because nse_equity_master failed in the eod step"
        )
    assert runs["nse_eod"].range_start == runs["nse_eod"].range_end == DAY
    assert {"nse_equity_master", "nse_eod", "kite_instruments"} <= set(result.failed_sources)


def test_a_source_that_fails_before_its_service_starts_is_still_recorded(
    conn: psycopg.Connection,
) -> None:
    def no_token() -> KiteSession:
        raise MissingSecret("KITE_ACCESS_TOKEN")

    result = collect_daily(
        conn,
        day=DAY,
        rule=RULE,
        feeds=FEEDS,
        transport=sources(),
        browser_transport=sources(),
        kite=no_token,
    )

    assert result.failed_sources == ("kite_candles_daily",)
    (candles,) = queries.ingest_runs(
        conn, collection_id=result.collection_id, source="kite_candles_daily"
    )
    assert candles.command == "collect-daily"
    assert candles.range_start == candles.range_end == DAY
    assert candles.error == "MissingSecret: secret KITE_ACCESS_TOKEN is not set"
    assert {r.source for r in result.runs} == SOURCES


def test_news_with_every_feed_failing_is_a_failed_source(conn: psycopg.Connection) -> None:
    result = collect(conn, FailingHost(sources(), "economictimes.indiatimes.com"))

    assert result.failed_sources == ("rss",)
    (news,) = queries.ingest_runs(conn, collection_id=result.collection_id, source="rss")
    assert news.error is not None and "every feed failed" in news.error
    assert news.details["failed_feeds"] == ["et_markets", "et_stocks"]


def test_the_command_records_every_source_it_could_not_reach_and_exits_non_zero(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under test the network is refused, so every source fails; the command still finishes."""
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_ROOT / "tradeline.toml"))
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)

    code = main(["collect-daily", "--date", "2024-08-01"])

    assert code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    collection = [line for line in lines if line["msg"].startswith("collect")]
    assert {"collection started", "collection finished", "collect-daily finished"} <= {
        line["msg"] for line in collection
    }
    assert all(
        (line["range_start"], line["range_end"]) == ("2024-08-01", "2024-08-01")
        for line in collection
    )
    last = lines[-1]
    assert last["command"] == "collect-daily"
    assert last["msg"] == "collect-daily finished"
    assert last["day"] == "2024-08-01"
    expected = [
        "nse_equity_master",
        "nse_eod",
        "kite_instruments",
        "kite_candles_daily",
        "nse_announcements",
        "bse_announcements",
        "rss",
    ]
    assert last["failed_sources"] == expected
    with app_connection(database_url) as conn:
        runs = queries.ingest_runs(conn, collection_id=uuid.UUID(str(last["collection_id"])))
    assert sorted(r.source for r in runs) == sorted(expected)
    assert {r.status for r in runs} == {"failed"}
