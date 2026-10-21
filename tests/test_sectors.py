"""``sectors`` against NSE's recorded index constituent lists, asserted through the query layer.

The fixture (``fixtures/sectors/``) is NSE's NIFTY Total Market list recorded
on 2026-10-21, trimmed to seven symbols. Its ``Industry`` column is NSE's
sector classification. A load is dated by the day it was taken; a later load
in which NSE has reclassified a symbol is made by playing the real recorded
response back through ``reclassify``, which rewrites one symbol's sector on its
way to the service, so no fixture file is edited and none is written by hand.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from playback import Playback, lines
from tradeline import queries
from tradeline.db import app_connection
from tradeline.sectors import load_sectors
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures" / "sectors"
FIRST_LOAD = date(2024, 7, 31)
SECOND_LOAD = date(2024, 9, 30)


def reclassify(symbol: str, sector: str) -> Playback:
    """The recorded lists played back with ``symbol``'s Industry column set to ``sector``."""

    def row(line: str) -> str:
        fields = line.split(",")
        if len(fields) == 5 and fields[2] == symbol:
            fields[1] = sector
        return ",".join(fields)

    return Playback(FIXTURES, edit=lines(lambda rows: [row(line) for line in rows]))


@pytest.fixture
def conn(fresh_database_url: str) -> Iterator[psycopg.Connection]:
    with app_connection(fresh_database_url) as conn:
        yield conn


def sector_of(conn: psycopg.Connection, symbol: str, as_of: date) -> str | None:
    found = queries.sector(conn, symbol, as_of=as_of)
    return None if found is None else found.sector


def test_a_load_records_each_symbols_sector_in_force_from_the_load_date(
    conn: psycopg.Connection,
) -> None:
    result = load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)

    assert (result.rows_seen, result.rows_written) == (7, 7)
    assert sector_of(conn, "INFY", FIRST_LOAD) == "Information Technology"
    assert sector_of(conn, "RELIANCE", date(2026, 1, 1)) == "Oil Gas & Consumable Fuels"
    assert sector_of(conn, "ABDL", FIRST_LOAD) == "Fast Moving Consumer Goods"
    found = queries.sector(conn, "HDFCBANK", as_of=FIRST_LOAD)
    assert found == queries.Sector(
        symbol="HDFCBANK", sector="Financial Services", effective_from=FIRST_LOAD
    )


def test_a_later_load_with_a_changed_sector_adds_a_dated_row_and_keeps_the_earlier_one(
    conn: psycopg.Connection,
) -> None:
    load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)

    result = load_sectors(conn, reclassify("INFY", "Capital Goods"), effective_date=SECOND_LOAD)

    assert result.rows_written == 1
    assert [(c.symbol, c.before, c.after) for c in result.changes] == [
        ("INFY", "Information Technology", "Capital Goods")
    ]
    assert sector_of(conn, "INFY", SECOND_LOAD) == "Capital Goods"
    assert sector_of(conn, "INFY", date(2025, 1, 1)) == "Capital Goods"
    # The earlier sector is still the one in force on any earlier date.
    assert sector_of(conn, "INFY", FIRST_LOAD) == "Information Technology"
    assert sector_of(conn, "INFY", date(2024, 9, 29)) == "Information Technology"
    # Symbols NSE did not reclassify keep their first row.
    assert queries.sector(conn, "TCS", as_of=SECOND_LOAD) == queries.Sector(
        symbol="TCS", sector="Information Technology", effective_from=FIRST_LOAD
    )


def test_reloading_an_unchanged_classification_writes_nothing(conn: psycopg.Connection) -> None:
    load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)

    again = load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=SECOND_LOAD)
    same_day = load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=SECOND_LOAD)

    assert (again.rows_seen, again.rows_written, again.changes) == (7, 0, ())
    assert (same_day.rows_written, same_day.anomalies) == (0, 0)
    assert queries.sector(conn, "INFY", as_of=SECOND_LOAD) == queries.Sector(
        symbol="INFY", sector="Information Technology", effective_from=FIRST_LOAD
    )


def test_no_sector_is_known_before_the_first_load(conn: psycopg.Connection) -> None:
    load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)

    assert queries.sector(conn, "INFY", as_of=date(2024, 7, 30)) is None
    assert queries.sector(conn, "NIFTYBEES", as_of=FIRST_LOAD) is None


def test_a_same_day_reload_that_disagrees_is_an_anomaly_and_the_stored_row_is_kept(
    conn: psycopg.Connection,
) -> None:
    load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)

    result = load_sectors(conn, reclassify("INFY", "Capital Goods"), effective_date=FIRST_LOAD)

    assert (result.rows_written, result.anomalies, result.changes) == (0, 1, ())
    assert sector_of(conn, "INFY", FIRST_LOAD) == "Information Technology"


def test_a_load_dated_before_the_latest_stored_one_is_refused(conn: psycopg.Connection) -> None:
    load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=SECOND_LOAD)

    with pytest.raises(ValueError, match="recorded from 2024-09-30"):
        load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)


def test_each_load_is_recorded_as_an_ingest_run(conn: psycopg.Connection) -> None:
    load_sectors(conn, PlaybackTransport(FIXTURES), effective_date=FIRST_LOAD)

    (run,) = queries.ingest_runs(conn, source="nse_sectors")
    assert (run.command, run.range_start, run.range_end) == ("sectors", FIRST_LOAD, FIRST_LOAD)
    assert (run.rows_seen, run.rows_written, run.error) == (7, 7, None)
