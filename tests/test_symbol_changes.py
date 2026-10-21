"""``ingest symbol-changes`` against NSE's recorded symbol-change list.

The fixture (``fixtures/instruments/``, see its README) is NSE's
``symbolchange.csv`` recorded on 2026-10-21 through the NSE session, trimmed to
eleven of its 1068 records: the renames of the #13 acceptance run (ZOMATO to
ETERNAL; LTI to LTIM to LTM; TELCO to TATAMOTORS to TMPV; MCDOWELL-N to
UNITDSPR; L&TFH to LTF; CADILAHC to ZYDUSLIFE) and three records as NSE writes
them: one with no company name, one whose name starts with a space, and one
whose old and new Symbol are the same.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.symbol_changes import SymbolChangeIngestResult, ingest_symbol_changes
from tradeline.sources.nse import SymbolChange
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures" / "instruments"
RECORDS = 11


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> tuple[SymbolChangeIngestResult, SymbolChangeIngestResult]:
    with app_connection(database_url) as conn:
        first = ingest_symbol_changes(conn, PlaybackTransport(FIXTURES))
        second = ingest_symbol_changes(conn, PlaybackTransport(FIXTURES))
    return first, second


@pytest.fixture
def conn(
    database_url: str, two_runs: tuple[SymbolChangeIngestResult, SymbolChangeIngestResult]
) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_every_record_is_stored_as_nse_published_it(
    conn: psycopg.Connection,
    two_runs: tuple[SymbolChangeIngestResult, SymbolChangeIngestResult],
) -> None:
    first, _ = two_runs
    run = queries.ingest_runs(conn, source="nse_symbol_changes")[-1]  # oldest

    assert (first.rows_seen, first.rows_written) == (RECORDS, RECORDS)
    assert (run.command, run.rows_seen, run.rows_written, run.error) == (
        "ingest",
        RECORDS,
        RECORDS,
        None,
    )
    stored = queries.symbol_changes(conn)
    assert len(stored) == RECORDS
    assert SymbolChange("ETERNAL LIMITED", "ZOMATO", "ETERNAL", date(2025, 4, 9)) in stored
    assert SymbolChange("LTIMindtree Limited", "LTI", "LTIM", date(2022, 12, 5)) in stored
    assert SymbolChange("LTM Limited", "LTIM", "LTM", date(2026, 2, 27)) in stored
    assert SymbolChange(None, "780LTFL30", "799LTFL30", date(2025, 2, 19)) in stored
    assert SymbolChange("Interise Trust", "INTERISE", "INTERISE", date(2024, 11, 12)) in stored
    assert (
        SymbolChange(
            "NIPPON INDIA MF - NIPPON INDIA Dual Advantage FTF  Sr. x Plan E -  GO",
            "RDAXEDG",
            "NDAXEDG",
            date(2019, 10, 30),
        )
        in stored
    )


def test_a_second_ingest_of_the_same_list_adds_nothing(
    conn: psycopg.Connection,
    two_runs: tuple[SymbolChangeIngestResult, SymbolChangeIngestResult],
) -> None:
    _, second = two_runs
    newest = queries.ingest_runs(conn, source="nse_symbol_changes")[0]

    assert (second.rows_seen, second.rows_written) == (RECORDS, 0)
    assert (newest.rows_seen, newest.rows_written) == (RECORDS, 0)
    assert len(queries.symbol_changes(conn)) == RECORDS


def test_the_records_of_one_symbol_are_found_under_its_old_or_new_name(
    conn: psycopg.Connection,
) -> None:
    assert [(c.old_symbol, c.new_symbol) for c in queries.symbol_changes(conn, symbol="LTIM")] == [
        ("LTI", "LTIM"),
        ("LTIM", "LTM"),
    ]
