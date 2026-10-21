"""``ingest candles --interval day`` against Kite fixtures, asserted through the query layer.

The instrument master comes from the recorded NSE and Kite fixtures of
``test_ingest_eod``. The candle fixtures themselves are Kite's documented
historical response shape filled with the real NSE end-of-day values for the
same five symbols and range (2024-07-01 to 2024-08-30): a true Kite recording
needs the Operator's daily access token, which a session cannot obtain. The
fixture directory's README says how to replace them with a real recording.

Time is a fake clock that advances only when the session sleeps, so the
spacing between calls is observable without waiting.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import psycopg
import pytest

from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.candles import CandleIngestResult, ingest_candles
from tradeline.ingest.eod import ingest_eod
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport, Request, Response

EOD_FIXTURES = Path(__file__).parent / "fixtures" / "ingest_eod"
FIXTURES = Path(__file__).parent / "fixtures" / "ingest_candles"
RESEND = Path(__file__).parent / "fixtures" / "ingest_candles_resend"
START, END = date(2024, 7, 1), date(2024, 8, 30)
TRADING_DAYS = 43
SYMBOLS = 4  # the EQ series symbols; BAFNAPH is BE and is loaded only when named


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TimedPlayback:
    """Playback that also notes the fake-clock time each request was sent at."""

    def __init__(self, fixture_dir: Path, clock: FakeClock) -> None:
        self._inner = PlaybackTransport(fixture_dir)
        self._clock = clock
        self.calls: list[tuple[float, Request]] = []

    def send(self, request: Request) -> Response:
        self.calls.append((self._clock.now, request))
        return self._inner.send(request)


class ChunkedKite:
    """Answers any historical request with the same bars; records the requests it sees."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.calls: list[Request] = []

    def send(self, request: Request) -> Response:
        self.calls.append(request)
        return Response(200, (("content-type", "application/json"),), self._body)


def kite_session(transport: TimedPlayback, clock: FakeClock) -> KiteSession:
    return KiteSession(
        transport, api_key="key", access_token="tok", clock=clock.time, sleep=clock.sleep
    )


@dataclass(frozen=True)
class TwoRuns:
    first: CandleIngestResult
    second: CandleIngestResult
    calls: list[tuple[float, Request]]


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> TwoRuns:
    """The master loaded, then the same candle range ingested twice."""
    clock = FakeClock()
    transport = TimedPlayback(FIXTURES, clock)
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(EOD_FIXTURES), start=START, end=END)
        first = ingest_candles(conn, kite_session(transport, clock), start=START, end=END)
        calls = list(transport.calls)
        second = ingest_candles(conn, kite_session(transport, clock), start=START, end=END)
    return TwoRuns(first, second, calls)


@pytest.fixture
def conn(database_url: str, two_runs: TwoRuns) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_a_fixture_backed_run_loads_a_bar_per_symbol_per_trading_day_and_records_it(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    run = queries.ingest_runs(conn, source="kite_candles_daily")[-1]  # oldest

    assert two_runs.first.rows_written == TRADING_DAYS * SYMBOLS
    assert two_runs.first.symbols_loaded == SYMBOLS
    assert run.command == "ingest"
    assert (run.range_start, run.range_end) == (START, END)
    assert run.rows_seen == TRADING_DAYS * SYMBOLS
    assert run.rows_written == TRADING_DAYS * SYMBOLS
    assert run.details["interval"] == "day"
    assert run.details["requests"] == SYMBOLS
    assert run.error is None


def test_the_call_log_shows_one_request_per_symbol_spaced_to_three_per_second(
    two_runs: TwoRuns,
) -> None:
    urls = [request.url for _, request in two_runs.calls]
    assert len(urls) == SYMBOLS
    assert all("/instruments/historical/" in url and "/day?" in url for url in urls)
    assert all(url.endswith("from=2024-07-01&to=2024-08-30") for url in urls)

    gaps = [later - earlier for (earlier, _), (later, _) in pairwise(two_runs.calls)]
    assert all(1 / 3 - 1e-9 <= gap < 0.5 for gap in gaps)


def test_a_second_run_over_the_same_range_writes_nothing_and_records_zero_new_rows(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    newest = queries.ingest_runs(conn, source="kite_candles_daily")[0]

    assert two_runs.second.rows_written == 0
    assert two_runs.second.anomalies == 0
    assert newest.rows_seen == TRADING_DAYS * SYMBOLS
    assert newest.rows_written == 0
    assert len(queries.daily_candles(conn, "INFY", start=START, end=END)) == TRADING_DAYS


def test_the_query_layer_returns_raw_daily_candles_for_a_symbol_and_range(
    conn: psycopg.Connection,
) -> None:
    week = queries.daily_candles(conn, "INFY", start=date(2024, 7, 1), end=date(2024, 7, 5))

    assert [bar.trade_date for bar in week] == [
        date(2024, 7, 1),
        date(2024, 7, 2),
        date(2024, 7, 3),
        date(2024, 7, 4),
        date(2024, 7, 5),
    ]
    first = week[0]
    assert (first.symbol, first.series) == ("INFY", "EQ")
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("1559.5"),
        Decimal("1599.9"),
        Decimal("1559.5"),
        Decimal("1590.8"),
    )
    assert first.volume == 6801771


def test_the_range_is_inclusive_and_a_symbol_outside_the_fixtures_has_no_bars(
    conn: psycopg.Connection,
) -> None:
    assert len(queries.daily_candles(conn, "BAJAJ-AUTO", start=START, end=END)) == TRADING_DAYS
    assert queries.daily_candles(conn, "INFY", start=date(2024, 6, 1), end=date(2024, 6, 30)) == []
    assert queries.daily_candles(conn, "KHADIM", start=START, end=END) == []


def test_a_be_series_symbol_is_not_loaded_by_default_but_is_when_named(
    database_url: str, two_runs: TwoRuns
) -> None:
    clock = FakeClock()
    transport = TimedPlayback(FIXTURES, clock)
    with app_connection(database_url) as conn:
        assert queries.daily_candles(conn, "BAFNAPH", start=START, end=END) == []
        result = ingest_candles(
            conn, kite_session(transport, clock), start=START, end=END, symbols=["BAFNAPH"]
        )
        everything = queries.daily_candles(conn, "BAFNAPH", start=START, end=END)
        be = queries.daily_candles(conn, "BAFNAPH", start=START, end=END, series="BE")
        eq = queries.daily_candles(conn, "BAFNAPH", start=START, end=END, series="EQ")

    assert result.rows_written == TRADING_DAYS
    assert len(everything) == len(be) == TRADING_DAYS
    assert eq == []


def test_a_range_longer_than_kites_limit_is_fetched_in_chunks_of_two_thousand_days(
    database_url: str, two_runs: TwoRuns
) -> None:
    """Kite answers one request with at most 2000 days; the service asks in pieces."""
    kite = ChunkedKite(b'{"status":"success","data":{"candles":[]}}')
    clock = FakeClock()
    session = KiteSession(
        kite, api_key="key", access_token="tok", clock=clock.time, sleep=clock.sleep
    )
    with app_connection(database_url) as conn:
        result = ingest_candles(
            conn, session, start=date(2019, 1, 1), end=date(2024, 8, 30), symbols=["INFY"]
        )

    assert result.requests == 2
    assert [request.url.split("?")[1] for request in kite.calls] == [
        "from=2019-01-01&to=2024-06-22",  # 2000 days inclusive
        "from=2024-06-23&to=2024-08-30",
    ]


def test_a_resent_bar_with_a_changed_close_keeps_the_stored_bar_and_records_an_anomaly(
    database_url: str, two_runs: TwoRuns
) -> None:
    """The resend fixture is INFY for 1 July 2024 with the close altered by hand."""
    day = date(2024, 7, 1)
    clock = FakeClock()
    transport = TimedPlayback(RESEND, clock)
    with app_connection(database_url) as conn:
        result = ingest_candles(
            conn, kite_session(transport, clock), start=day, end=day, symbols=["INFY"]
        )
        (infy,) = queries.daily_candles(conn, "INFY", start=day, end=day)
        run = queries.ingest_runs(conn, source="kite_candles_daily")[0]

    assert (result.rows_written, result.anomalies) == (0, 1)
    assert infy.close == Decimal("1590.8")
    assert run.details["anomalies"] == 1
    assert run.details["anomaly_bars"] == [
        {
            "symbol": "INFY",
            "series": "EQ",
            "trade_date": "2024-07-01",
            "stored": {
                "open": "1559.5",
                "high": "1599.9",
                "low": "1559.5",
                "close": "1590.8",
                "volume": 6801771,
            },
            "received": {
                "open": "1559.5",
                "high": "1599.9",
                "low": "1559.5",
                "close": "1591.8",
                "volume": 6801771,
            },
        }
    ]


def test_symbols_can_be_narrowed_and_an_unknown_symbol_is_refused(
    database_url: str, two_runs: TwoRuns
) -> None:
    clock = FakeClock()
    transport = TimedPlayback(FIXTURES, clock)
    with app_connection(database_url) as conn:
        result = ingest_candles(
            conn, kite_session(transport, clock), start=START, end=END, symbols=["TCS", "INFY"]
        )
        with pytest.raises(ValueError, match="NOSUCH"):
            ingest_candles(
                conn, kite_session(transport, clock), start=START, end=END, symbols=["NOSUCH"]
            )

    assert result.symbols_loaded == 2
    assert len(transport.calls) == 2


def test_a_symbol_without_a_kite_token_is_skipped_and_reported(
    database_url: str, two_runs: TwoRuns
) -> None:
    clock = FakeClock()
    transport = TimedPlayback(FIXTURES, clock)
    with app_connection(database_url) as conn:
        # Setup, not an assertion: the fixtures hold no symbol Kite's dump lacks.
        conn.execute("INSERT INTO symbols (symbol, series) VALUES ('NOTOKEN', 'EQ')")
        conn.commit()
        result = ingest_candles(
            conn, kite_session(transport, clock), start=START, end=END, symbols=["NOTOKEN", "TCS"]
        )
        run = queries.ingest_runs(conn, source="kite_candles_daily")[0]

    assert result.symbols_loaded == 1
    assert result.symbols_without_token == ("NOTOKEN",)
    assert run.details["symbols_without_token"] == ["NOTOKEN"]


def test_a_failed_run_keeps_the_symbols_already_loaded_and_records_the_error(
    database_url: str, tmp_path: Path, two_runs: TwoRuns
) -> None:
    clock = FakeClock()
    transport = TimedPlayback(tmp_path, clock)  # no fixtures at all
    with app_connection(database_url) as conn:
        with pytest.raises(Exception, match="no fixture for"):
            ingest_candles(conn, kite_session(transport, clock), start=START, end=END)
        failed = queries.ingest_runs(conn, source="kite_candles_daily")[0]

    assert failed.error is not None and "UnrecordedRequest" in failed.error
    assert failed.rows_written == 0


def test_a_range_that_ends_before_it_starts_is_refused(database_url: str) -> None:
    clock = FakeClock()
    transport = TimedPlayback(FIXTURES, clock)
    with app_connection(database_url) as conn, pytest.raises(ValueError, match="before start"):
        ingest_candles(conn, kite_session(transport, clock), start=END, end=START)
