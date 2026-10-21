"""``ingest candles --interval minute`` against Kite fixtures, asserted through the query layer.

The instrument master and the Universe come from the recorded NSE files of
``fixtures/universe/``: under a two-member rule, August 2024's Universe is
RELIANCE and INFY. The candle fixtures are real Kite minute responses for
those two symbols on 31 July and 1 August 2024, so one run crosses a month
boundary and lands in two monthly partitions. Kite adjusts minute bars as it
does daily ones; the values asserted are Kite's.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.candles import CandleIngestResult, CurrentUniverse, ingest_candles
from tradeline.ingest.eod import ingest_eod
from tradeline.sources.kite import INTERVAL_MINUTE, KiteSession
from tradeline.transport import PlaybackTransport, Request, Response
from tradeline.universe import compute_universe

UNIVERSE_FIXTURES = Path(__file__).parent / "fixtures" / "universe"
FIXTURES = Path(__file__).parent / "fixtures" / "ingest_minute_candles"
RESEND = Path(__file__).parent / "fixtures" / "ingest_minute_candles_resend"
IST = ZoneInfo("Asia/Kolkata")
AUGUST = date(2024, 8, 1)
JULY_31 = date(2024, 7, 31)
RULE = UniverseRule(top_n=2, lookback_trading_days=20, min_listing_trading_days=21)
UNIVERSE = CurrentUniverse(month=AUGUST, rule_version=RULE.version)
BARS_PER_DAY = 375  # 09:15 to 15:29


def ist(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


class NoWaitClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def kite_session(transport: object) -> KiteSession:
    clock = NoWaitClock()
    return KiteSession(
        transport,  # type: ignore[arg-type]
        api_key="key",
        access_token="tok",
        clock=clock.time,
        sleep=clock.sleep,
    )


@dataclass(frozen=True)
class TwoRuns:
    first: CandleIngestResult
    second: CandleIngestResult
    calls: list[Request]
    partitions_before: list[queries.MinutePartition]


def load_universe(conn: psycopg.Connection) -> None:
    ingest_eod(conn, PlaybackTransport(UNIVERSE_FIXTURES), start=date(2024, 7, 1), end=JULY_31)
    compute_universe(conn, RULE, month=AUGUST)


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> TwoRuns:
    """The master and August's Universe loaded, then the same two days ingested twice."""
    transport = PlaybackTransport(FIXTURES)
    with app_connection(database_url) as conn:
        load_universe(conn)
        before = queries.minute_candle_partitions(conn)
        first = ingest_candles(
            conn,
            kite_session(transport),
            start=JULY_31,
            end=AUGUST,
            interval=INTERVAL_MINUTE,
            universe=UNIVERSE,
        )
        calls = list(transport.calls)
        second = ingest_candles(
            conn,
            kite_session(transport),
            start=JULY_31,
            end=AUGUST,
            interval=INTERVAL_MINUTE,
            universe=UNIVERSE,
        )
    return TwoRuns(first, second, calls, before)


@pytest.fixture
def conn(database_url: str, two_runs: TwoRuns) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_the_current_universe_is_loaded_into_one_partition_per_month(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    assert two_runs.first.symbols_loaded == 2
    assert two_runs.first.rows_written == 2 * 2 * BARS_PER_DAY
    assert [(p.month, p.rows) for p in queries.minute_candle_partitions(conn)] == [
        (date(2024, 7, 1), 2 * BARS_PER_DAY),
        (date(2024, 8, 1), 2 * BARS_PER_DAY),
    ]


def test_a_months_partition_is_created_on_its_first_insert(two_runs: TwoRuns) -> None:
    assert two_runs.partitions_before == []


def test_one_request_per_universe_symbol_covers_both_days(two_runs: TwoRuns) -> None:
    urls = sorted(request.url for request in two_runs.calls)
    assert urls == [
        "https://api.kite.trade/instruments/historical/408065/minute?from=2024-07-31&to=2024-08-01",
        "https://api.kite.trade/instruments/historical/738561/minute?from=2024-07-31&to=2024-08-01",
    ]


def test_the_run_is_recorded_under_the_minute_source(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    run = queries.ingest_runs(conn, source="kite_candles_minute")[-1]  # oldest

    assert (run.range_start, run.range_end) == (JULY_31, AUGUST)
    assert run.rows_written == 4 * BARS_PER_DAY
    assert run.details["interval"] == "minute"
    assert run.details["universe"] == {"month": "2024-08-01", "rule_version": RULE.version}
    assert run.details["requests"] == 2
    assert run.error is None


def test_re_running_the_same_days_inserts_nothing(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    newest = queries.ingest_runs(conn, source="kite_candles_minute")[0]

    assert (two_runs.second.rows_written, two_runs.second.anomalies) == (0, 0)
    assert (newest.rows_seen, newest.rows_written) == (4 * BARS_PER_DAY, 0)
    assert [p.rows for p in queries.minute_candle_partitions(conn)] == [750, 750]


def test_the_query_layer_returns_every_bar_of_a_symbol_and_day_in_time_order(
    conn: psycopg.Connection,
) -> None:
    bars = queries.minute_candles(conn, "INFY", day=JULY_31)

    assert len(bars) == BARS_PER_DAY
    assert [bar.ts for bar in bars] == sorted(bar.ts for bar in bars)
    assert (bars[0].ts, bars[-1].ts) == (ist(JULY_31, 9, 15), ist(JULY_31, 15, 29))
    first = bars[0]
    assert (first.symbol, first.series) == ("INFY", "EQ")
    # Values as Kite returned them for 31 July 2024 09:15 IST on 2026-10-21.
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        Decimal("1837.2"),
        Decimal("1845.2"),
        Decimal("1832.5"),
        Decimal("1842.6"),
        81150,
    )


def test_a_day_is_the_indian_trading_day_and_holds_only_its_own_bars(
    conn: psycopg.Connection,
) -> None:
    august = queries.minute_candles(conn, "RELIANCE", day=AUGUST)

    assert len(august) == BARS_PER_DAY
    assert {bar.ts.astimezone(IST).date() for bar in august} == {AUGUST}
    assert (august[0].open, august[-1].close) == (Decimal("1512"), Decimal("1517.95"))
    assert queries.minute_candles(conn, "RELIANCE", day=date(2024, 8, 2)) == []
    assert queries.minute_candles(conn, "TCS", day=AUGUST) == []


def test_without_a_universe_for_the_month_the_run_is_refused(database_url: str) -> None:
    with (
        app_connection(database_url) as conn,
        pytest.raises(ValueError, match="no Universe for 2024-09"),
    ):
        ingest_candles(
            conn,
            kite_session(PlaybackTransport(FIXTURES)),
            start=JULY_31,
            end=AUGUST,
            interval=INTERVAL_MINUTE,
            universe=CurrentUniverse(month=date(2024, 9, 1), rule_version=RULE.version),
        )


class EmptyKite:
    """Answers any historical request with no bars; records the requests it sees."""

    def __init__(self) -> None:
        self.calls: list[Request] = []

    def send(self, request: Request) -> Response:
        self.calls.append(request)
        body = b'{"status":"success","data":{"candles":[]}}'
        return Response(200, (("content-type", "application/json"),), body)


def test_a_range_longer_than_kites_limit_is_fetched_in_chunks_of_sixty_days(
    database_url: str, two_runs: TwoRuns
) -> None:
    """Kite refuses a minute request spanning more than 60 days (checked live)."""
    kite = EmptyKite()
    with app_connection(database_url) as conn:
        result = ingest_candles(
            conn,
            kite_session(kite),
            start=date(2024, 5, 1),
            end=date(2024, 8, 30),
            interval=INTERVAL_MINUTE,
            symbols=["INFY"],
        )

    assert result.requests == 3
    assert [request.url.split("?")[1] for request in kite.calls] == [
        "from=2024-05-01&to=2024-06-29",  # 60 days inclusive
        "from=2024-06-30&to=2024-08-28",
        "from=2024-08-29&to=2024-08-30",
    ]


def test_a_resent_minute_bar_with_a_changed_close_keeps_the_stored_bar_and_is_an_anomaly(
    database_url: str, two_runs: TwoRuns
) -> None:
    """The resend fixture is INFY's real 31 July response with the 09:15 close altered."""
    with app_connection(database_url) as conn:
        result = ingest_candles(
            conn,
            kite_session(PlaybackTransport(RESEND)),
            start=JULY_31,
            end=JULY_31,
            interval=INTERVAL_MINUTE,
            symbols=["INFY"],
        )
        first = queries.minute_candles(conn, "INFY", day=JULY_31)[0]
        run = queries.ingest_runs(conn, source="kite_candles_minute")[0]

    assert (result.rows_written, result.anomalies) == (0, 1)
    assert first.close == Decimal("1842.6")
    assert run.details["anomaly_bars"] == [
        {
            "symbol": "INFY",
            "series": "EQ",
            "ts": "2024-07-31T09:15:00+05:30",
            "stored": {
                "open": "1837.2",
                "high": "1845.2",
                "low": "1832.5",
                "close": "1842.6",
                "volume": 81150,
            },
            "received": {
                "open": "1837.2",
                "high": "1845.2",
                "low": "1832.5",
                "close": "1843.6",
                "volume": 81150,
            },
        }
    ]


def test_symbols_and_a_universe_cannot_both_be_given(database_url: str) -> None:
    with app_connection(database_url) as conn, pytest.raises(ValueError, match="not both"):
        ingest_candles(
            conn,
            kite_session(PlaybackTransport(FIXTURES)),
            start=JULY_31,
            end=AUGUST,
            interval=INTERVAL_MINUTE,
            symbols=["INFY"],
            universe=UNIVERSE,
        )
