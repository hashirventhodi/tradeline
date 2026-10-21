"""``ingest eod`` against recorded NSE and Kite fixtures, asserted through the query layer.

The fixture range is 2024-07-01 to 2024-08-30, trimmed to five symbols: four in
the EQ series (one with a hyphen in the symbol) and one in BE. It spans the
legacy-to-UDiFF file format change on 2024-07-08 and two exchange holidays.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.eod import EodIngestResult, ingest_eod
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures" / "ingest_eod"
START, END = date(2024, 7, 1), date(2024, 8, 30)
TRADING_DAYS = 43  # 45 weekdays less Muharram (17 Jul) and Independence Day (15 Aug)
SYMBOLS = 5


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> tuple[EodIngestResult, EodIngestResult]:
    """The same range ingested twice; every test reads the state this leaves."""
    with app_connection(database_url) as conn:
        first = ingest_eod(conn, PlaybackTransport(FIXTURES), start=START, end=END)
        second = ingest_eod(conn, PlaybackTransport(FIXTURES), start=START, end=END)
    return first, second


@pytest.fixture
def conn(
    database_url: str, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_a_fixture_backed_run_loads_rows_and_records_a_run_with_counts(
    conn: psycopg.Connection,
) -> None:
    eod_runs = queries.ingest_runs(conn, source="nse_eod")
    first = eod_runs[-1]  # oldest

    assert first.command == "ingest"
    assert (first.range_start, first.range_end) == (START, END)
    assert first.rows_seen == TRADING_DAYS * SYMBOLS
    assert first.rows_written == TRADING_DAYS * SYMBOLS
    assert first.error is None
    assert first.finished_at >= first.started_at


def test_a_second_run_over_the_same_range_inserts_nothing_and_records_zero_new_rows(
    conn: psycopg.Connection, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> None:
    _, second = two_runs
    newest, _oldest = queries.ingest_runs(conn, source="nse_eod")[:2]

    assert second.rows_written == 0
    assert second.symbols_written == 0
    assert newest.rows_seen == TRADING_DAYS * SYMBOLS
    assert newest.rows_written == 0
    assert newest.error is None
    assert len(queries.end_of_day(conn, "INFY", start=START, end=END)) == TRADING_DAYS


def test_the_query_layer_returns_end_of_day_rows_for_a_symbol_and_range(
    conn: psycopg.Connection,
) -> None:
    week = queries.end_of_day(conn, "INFY", start=date(2024, 7, 1), end=date(2024, 7, 5))

    assert [row.trade_date for row in week] == [
        date(2024, 7, 1),
        date(2024, 7, 2),
        date(2024, 7, 3),
        date(2024, 7, 4),
        date(2024, 7, 5),
    ]
    # Values as NSE's legacy-format file states them for 1 July 2024.
    first = week[0]
    assert (first.series, first.isin) == ("EQ", "INE009A01021")
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("1559.5"),
        Decimal("1599.9"),
        Decimal("1559.5"),
        Decimal("1590.8"),
    )
    assert (first.last, first.prev_close) == (Decimal("1586"), Decimal("1566.75"))
    assert (first.traded_qty, first.traded_value, first.trades) == (
        6801771,
        Decimal("10805277301.65"),
        244645,
    )


def test_rows_from_the_udiff_format_after_the_cutover_read_the_same_way(
    conn: psycopg.Connection,
) -> None:
    (row,) = queries.end_of_day(conn, "INFY", start=date(2024, 8, 30), end=date(2024, 8, 30))

    # Values as NSE's UDiFF file states them for 30 August 2024.
    assert (row.open, row.high, row.low, row.close) == (
        Decimal("1938.00"),
        Decimal("1950.00"),
        Decimal("1928.00"),
        Decimal("1943.70"),
    )
    assert (row.traded_qty, row.traded_value, row.trades) == (
        13466759,
        Decimal("26134516922.15"),
        246824,
    )


def test_the_range_is_inclusive_and_a_symbol_outside_the_fixtures_has_no_rows(
    conn: psycopg.Connection,
) -> None:
    assert len(queries.end_of_day(conn, "BAJAJ-AUTO", start=START, end=END)) == TRADING_DAYS
    assert queries.end_of_day(conn, "INFY", start=date(2024, 6, 1), end=date(2024, 6, 30)) == []
    assert queries.end_of_day(conn, "KHADIM", start=START, end=END) == []


def test_series_can_be_narrowed_so_a_be_series_symbol_has_no_eq_rows(
    conn: psycopg.Connection,
) -> None:
    be = queries.end_of_day(conn, "BAFNAPH", start=START, end=END, series="BE")
    eq = queries.end_of_day(conn, "BAFNAPH", start=START, end=END, series="EQ")

    assert len(be) == TRADING_DAYS
    assert eq == []


def test_symbols_carry_isin_series_listing_date_and_kite_token_for_every_fixture_symbol(
    conn: psycopg.Connection,
) -> None:
    master = queries.symbols(conn)

    assert [(s.symbol, s.series) for s in master] == [
        ("BAFNAPH", "BE"),
        ("BAJAJ-AUTO", "EQ"),
        ("INFY", "EQ"),
        ("RELIANCE", "EQ"),
        ("TCS", "EQ"),
    ]
    assert all(s.isin and s.listing_date and s.kite_instrument_token for s in master)
    (infy,) = queries.symbols(conn, symbol="INFY")
    assert infy == queries.Symbol(
        symbol="INFY",
        series="EQ",
        isin="INE009A01021",
        name="Infosys Limited",
        listing_date=date(1995, 2, 8),
        kite_instrument_token=408065,
    )
    (bafnaph,) = queries.symbols(conn, symbol="BAFNAPH")
    assert (bafnaph.isin, bafnaph.kite_instrument_token) == ("INE878I01022", 4540161)
    (bajaj,) = queries.symbols(conn, symbol="BAJAJ-AUTO")
    assert bajaj.kite_instrument_token == 4267265


def test_a_trading_day_without_a_file_is_reported_in_the_run_not_skipped_silently(
    conn: psycopg.Connection, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> None:
    first, _ = two_runs
    run = queries.ingest_runs(conn, source="nse_eod")[-1]

    assert first.missing_days == (date(2024, 7, 17), date(2024, 8, 15))
    assert first.days_loaded == TRADING_DAYS
    assert run.details["missing_days"] == ["2024-07-17", "2024-08-15"]
    assert run.details["days_loaded"] == TRADING_DAYS


def test_a_run_that_fails_is_recorded_with_its_error(
    database_url: str, tmp_path: Path, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> None:
    with app_connection(database_url) as conn:
        with pytest.raises(Exception, match="no fixture for"):
            ingest_eod(conn, PlaybackTransport(tmp_path), start=START, end=END)
        failed = queries.ingest_runs(conn)[0]

    assert failed.source == "nse_equity_master"
    assert failed.error is not None and "UnrecordedRequest" in failed.error
    assert failed.rows_written == 0


def test_a_range_that_ends_before_it_starts_is_refused(database_url: str) -> None:
    with app_connection(database_url) as conn, pytest.raises(ValueError, match="before start"):
        ingest_eod(conn, PlaybackTransport(FIXTURES), start=END, end=START)
