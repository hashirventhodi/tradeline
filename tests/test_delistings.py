"""``ingest delistings`` against NSE's recorded list of delisted securities.

The fixture (``fixtures/end_reasons/``, see its README) is NSE's
``delisted.csv`` recorded on 2026-10-21 through the NSE session, trimmed to
five of its 328 records: the first, one of each kind of delisting, one whose
company name holds the non-breaking space of NSE's Windows encoding, and the
last (HEXAWARE, 9 November 2020; NSE last changed the file on 11 November 2020).
"""

from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from playback import Playback
from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.delistings import DelistingIngestResult, ingest_delistings
from tradeline.sources.nse import DELISTINGS_URL, Delisting, NseError
from tradeline.transport import PlaybackTransport, Request

FIXTURES = Path(__file__).parent / "fixtures" / "end_reasons"
RECORDS = 5


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> tuple[DelistingIngestResult, DelistingIngestResult]:
    with app_connection(database_url) as conn:
        first = ingest_delistings(conn, PlaybackTransport(FIXTURES))
        second = ingest_delistings(conn, PlaybackTransport(FIXTURES))
    return first, second


@pytest.fixture
def conn(
    database_url: str, two_runs: tuple[DelistingIngestResult, DelistingIngestResult]
) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_every_record_is_stored_as_nse_published_it(
    conn: psycopg.Connection,
    two_runs: tuple[DelistingIngestResult, DelistingIngestResult],
) -> None:
    first, _ = two_runs
    run = queries.ingest_runs(conn, source="nse_delistings")[-1]  # oldest

    assert (first.rows_seen, first.rows_written) == (RECORDS, RECORDS)
    assert (run.command, run.rows_seen, run.rows_written, run.error) == (
        "ingest",
        RECORDS,
        RECORDS,
        None,
    )
    assert queries.delistings(conn) == [
        Delisting("CABOTINDIA", "Cabot India Ltd", date(2002, 4, 15), "Voluntary Delisting"),
        Delisting(
            "ANIKSHIP",
            "Anik Ship Breaking Industries Ltd.",
            date(2004, 7, 26),
            "Compulsory Delisting",
        ),
        Delisting(
            "BENTOLCHEM",
            "Chemox\u00a0Laboratories Limited",
            date(2016, 11, 22),
            "Compulsory Delisting",
        ),
        Delisting("ANGIND", "ANG Industries Limited", date(2020, 7, 10), "Delisting - Liquidation"),
        Delisting(
            "HEXAWARE", "Hexaware Technologies Limited", date(2020, 11, 9), "Voluntary Delisting"
        ),
    ]


def test_a_second_ingest_of_the_same_list_adds_nothing(
    conn: psycopg.Connection,
    two_runs: tuple[DelistingIngestResult, DelistingIngestResult],
) -> None:
    _, second = two_runs

    assert (second.rows_seen, second.rows_written) == (RECORDS, 0)
    assert len(queries.delistings(conn)) == RECORDS


def test_one_symbols_records_are_found_by_symbol(conn: psycopg.Connection) -> None:
    assert [d.delisted_on for d in queries.delistings(conn, symbol="ANGIND")] == [date(2020, 7, 10)]


def _list_body(change: Callable[[bytes], bytes]) -> Callable[[Request, bytes], bytes]:
    """NSE's recorded list with ``change`` made to its body."""

    def edit(request: Request, body: bytes) -> bytes:
        return change(body) if request.url == DELISTINGS_URL else body

    return edit


def test_a_list_sent_as_utf8_with_a_byte_order_mark_is_read_the_same(
    fresh_database_url: str,
) -> None:
    as_utf8 = _list_body(lambda body: body.decode("cp1252").encode("utf-8-sig"))
    with app_connection(fresh_database_url) as conn:
        ingest_delistings(conn, Playback(FIXTURES, edit=as_utf8))
        stored = queries.delistings(conn)

    assert [d.symbol for d in stored] == [
        "CABOTINDIA",
        "ANIKSHIP",
        "BENTOLCHEM",
        "ANGIND",
        "HEXAWARE",
    ]
    assert stored[2].company_name == "Chemox\u00a0Laboratories Limited"


def test_a_byte_neither_utf8_nor_windows_encoding_defines_fails_the_run_naming_it(
    fresh_database_url: str,
) -> None:
    # 0x81 is undefined in Windows-1252, and alone it is not UTF-8.
    undefined = _list_body(lambda body: body.replace(b"\xa0", b"\x81"))
    with app_connection(fresh_database_url) as conn:
        with pytest.raises(NseError, match="0x81"):
            ingest_delistings(conn, Playback(FIXTURES, edit=undefined))
        failed = queries.ingest_runs(conn, source="nse_delistings")[0]
        stored = queries.delistings(conn)

    assert failed.error is not None and "NseError" in failed.error
    assert stored == []
