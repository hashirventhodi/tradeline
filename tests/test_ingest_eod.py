"""``ingest eod`` against recorded NSE and Kite fixtures, asserted through the query layer.

The fixture range is 2024-07-01 to 2024-08-30, trimmed to five symbols: four in
the EQ series (one with a hyphen in the symbol) and one in BE. It spans the
legacy-to-UDiFF file format change on 2024-07-08, two exchange holidays, and
the weekends in between (all 404s from NSE).
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
from tradeline.sources.nse import NseSession, fetch_bhavcopy
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures" / "ingest_eod"
START, END = date(2024, 7, 1), date(2024, 8, 30)
TRADING_DAYS = 43  # 45 weekdays less Muharram (17 Jul) and Independence Day (15 Aug)
RESEND = Path(__file__).parent / "fixtures" / "ingest_eod_resend"
# NSE's legacy file for 2020-07-13, trimmed to three symbols.
TWO_DIGIT_YEAR = Path(__file__).parent / "fixtures" / "ingest_eod_two_digit_year"
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

    assert second.eod_rows_written == 0
    assert second.symbol_updates == 0
    assert second.anomalies == 0
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


def test_a_weekday_without_a_file_is_reported_in_the_run_and_weekends_are_not(
    conn: psycopg.Connection, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> None:
    first, _ = two_runs
    run = queries.ingest_runs(conn, source="nse_eod")[-1]

    assert first.missing_days == (date(2024, 7, 17), date(2024, 8, 15))
    assert first.days_loaded == TRADING_DAYS
    assert run.details["missing_days"] == ["2024-07-17", "2024-08-15"]
    assert run.details["days_loaded"] == TRADING_DAYS


def test_a_resent_file_with_different_values_is_an_anomaly_and_the_stored_row_is_kept(
    database_url: str, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> None:
    """The resend fixture is 1 July 2024 with INFY's close altered by hand."""
    day = date(2024, 7, 1)
    with app_connection(database_url) as conn:
        result = ingest_eod(conn, PlaybackTransport(RESEND), start=day, end=day)
        (infy,) = queries.end_of_day(conn, "INFY", start=day, end=day)
        run = queries.ingest_runs(conn, source="nse_eod")[0]

    assert (result.eod_rows_written, result.anomalies) == (0, 1)
    assert run.details["anomalies"] == 1
    assert infy.close == Decimal("1590.8")


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


def test_an_interrupted_run_is_recorded_as_an_error_not_a_success(
    database_url: str, two_runs: tuple[EodIngestResult, EodIngestResult]
) -> None:
    class Interrupting:
        def send(self, request: object) -> object:
            raise KeyboardInterrupt

    with app_connection(database_url) as conn:
        with pytest.raises(KeyboardInterrupt):
            ingest_eod(conn, Interrupting(), start=START, end=END)
        interrupted = queries.ingest_runs(conn)[0]

    assert interrupted.error == "KeyboardInterrupt: "
    assert interrupted.rows_written == 0


def test_a_legacy_file_dated_with_a_two_digit_year_reads_the_same_way() -> None:
    """NSE's legacy file for 13 July 2020 writes its TIMESTAMP as ``13-Jul-20``."""
    session = NseSession(PlaybackTransport(TWO_DIGIT_YEAR))

    rows = fetch_bhavcopy(session, date(2020, 7, 13))

    assert rows is not None
    assert [(r.trade_date, r.symbol, r.close, r.traded_qty) for r in rows] == [
        (date(2020, 7, 13), "20MICRONS", Decimal("33.45"), 187_303),
        (date(2020, 7, 13), "INFY", Decimal("797.05"), 10_804_911),
        (date(2020, 7, 13), "RELIANCE", Decimal("1935"), 32_124_397),
    ]
