"""Instruments rebuilt from the stored evidence, read through ``queries``.

The evidence is recorded (``fixtures/instruments/``, see its README): NSE's
end-of-day files for 7 to 11 April 2025 trimmed to ZOMATO and ETERNAL (ZOMATO
trades to the 8th, ETERNAL from the 9th; the 10th was a holiday), NSE's
equity master trimmed to ETERNAL (listed 23 July 2021, as ZOMATO), and the
trimmed symbol-change list of ``test_symbol_changes``. Kite's instrument list
comes from ``fixtures/ingest_eod/``.

The other stored sources (the July and August 2024 end-of-day rows, Kite's
candles, the Corporate Actions, and sectors) are loaded from their own
recorded fixtures to show that a rebuild leaves every stored row as it was.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from playback import Playback
from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.instruments import ManualLink
from tradeline.rebuild_instruments import InstrumentRebuildResult, rebuild_instruments
from tradeline.sectors import load_sectors
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = FIXTURES_DIR / "instruments"
EOD_FIXTURES = FIXTURES_DIR / "ingest_eod"
START, END = date(2025, 4, 7), date(2025, 4, 11)
RENAMED = date(2025, 4, 9)  # ZOMATO to ETERNAL
ETERNAL = "ZOMATO@2021-07-23"


@pytest.fixture(scope="module")
def rebuilt(database_url: str) -> InstrumentRebuildResult:
    with app_connection(database_url) as conn:
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=START, end=END)
        ingest_symbol_changes(conn, PlaybackTransport(FIXTURES))
        return rebuild_instruments(conn)


@pytest.fixture
def conn(database_url: str, rebuilt: InstrumentRebuildResult) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_a_symbol_resolves_to_the_instrument_it_named_on_a_date_on_either_side_of_a_rename(
    conn: psycopg.Connection,
) -> None:
    before = queries.resolve_symbol(conn, "ZOMATO", as_of=date(2025, 4, 8))
    after = queries.resolve_symbol(conn, "ETERNAL", as_of=RENAMED)

    assert before.key == after.key == ETERNAL
    # A Symbol no longer traded resolves to the Instrument it last named.
    assert queries.resolve_symbol(conn, "ZOMATO", as_of=date(2026, 10, 21)).key == ETERNAL
    # A day with no file resolves as the trading day before it.
    assert queries.resolve_symbol(conn, "ETERNAL", as_of=date(2025, 4, 10)).key == ETERNAL


def test_a_symbol_is_refused_before_it_named_any_instrument(conn: psycopg.Connection) -> None:
    with pytest.raises(LookupError, match="ETERNAL named no Instrument on or before 2025-04-08"):
        queries.resolve_symbol(conn, "ETERNAL", as_of=date(2025, 4, 8))
    with pytest.raises(LookupError, match="NOSUCH"):
        queries.resolve_symbol(conn, "NOSUCH", as_of=END)


def test_an_instruments_symbol_history_runs_across_the_rename_with_its_link(
    conn: psycopg.Connection,
) -> None:
    history = queries.instrument_history(conn, ETERNAL)

    assert (history.key, history.first_symbol, history.listed_on, history.key_from) == (
        ETERNAL,
        "ZOMATO",
        date(2021, 7, 23),
        "listing_date",
    )
    assert [(v.symbol, v.series, v.isin, v.first, v.last) for v in history.intervals] == [
        ("ZOMATO", "EQ", "INE758T01015", START, date(2025, 4, 8)),
        ("ETERNAL", "EQ", "INE758T01015", RENAMED, END),
    ]
    (link,) = history.links
    assert (link.kind, link.on, link.from_symbol, link.to_symbol, link.source) == (
        "symbol_change",
        RENAMED,
        "ZOMATO",
        "ETERNAL",
        "NSE symbol change ZOMATO to ETERNAL effective 2025-04-09",
    )


def test_an_unknown_instrument_key_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(LookupError, match="ETERNAL@2025-04-09"):
        queries.instrument_history(conn, "ETERNAL@2025-04-09")


def test_a_rebuild_records_its_counts_in_the_run_history(
    conn: psycopg.Connection, rebuilt: InstrumentRebuildResult
) -> None:
    (run,) = queries.ingest_runs(conn, source="instrument_links")

    assert (rebuilt.spans, rebuilt.instruments, rebuilt.links, rebuilt.anomalies) == (2, 1, 1, 0)
    assert (run.command, run.rows_seen, run.rows_written, run.error) == ("instruments", 2, 1, None)
    assert run.details == {
        "spans": 2,
        "symbol_changes": 11,
        "manual_links": 0,
        "instruments": 1,
        "links": {"symbol_change": 1},
        "keys_from": {"listing_date": 1},
        "anomalies": {},
        "candidates": 0,
    }


def test_rebuilding_again_replaces_the_instruments_with_identical_ones(
    conn: psycopg.Connection,
) -> None:
    before = queries.instrument_history(conn, ETERNAL)

    again = rebuild_instruments(conn)

    assert (again.instruments, again.links) == (1, 1)
    assert queries.instrument_history(conn, ETERNAL) == before


STORED = ("eod_bhavcopy", "candles_daily", "corporate_actions", "sectors", "symbol_changes")


def stored_rows(conn: psycopg.Connection) -> dict[str, tuple[int, str]]:
    """Each evidence table's row count and a digest of every row's text."""
    found = {}
    for table in STORED:
        row = conn.execute(
            f"SELECT count(*), coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '')"
            f" FROM {table} t"
        ).fetchone()
        assert row is not None
        found[table] = (int(row[0]), str(row[1]))
    return found


def test_stored_rows_are_byte_identical_before_and_after_a_build(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        july, august = date(2024, 7, 1), date(2024, 8, 30)
        ingest_eod(conn, PlaybackTransport(EOD_FIXTURES), start=july, end=august)
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=START, end=END)
        session = KiteSession(
            PlaybackTransport(FIXTURES_DIR / "ingest_candles"),
            api_key="key",
            access_token="tok",
            sleep=lambda _: None,
        )
        ingest_candles(conn, session, start=july, end=august, symbols=["RELIANCE"])
        ingest_corporate_actions(
            conn,
            PlaybackTransport(FIXTURES_DIR / "ingest_corporate_actions"),
            start=july,
            end=date(2024, 12, 31),
        )
        load_sectors(conn, PlaybackTransport(FIXTURES_DIR / "sectors"), effective_date=august)
        ingest_symbol_changes(conn, PlaybackTransport(FIXTURES))
        before = stored_rows(conn)
        assert all(count > 0 for count, _ in before.values()), before

        rebuild_instruments(conn)
        queries.instrument_history(conn, queries.resolve_symbol(conn, "RELIANCE", as_of=END).key)

        assert stored_rows(conn) == before


def test_a_manual_link_joins_a_rename_with_no_nse_record_its_citation_stored_as_the_source(
    fresh_database_url: str,
) -> None:
    # Without NSE's symbol-change list, only the manual link joins ZOMATO to ETERNAL.
    citation = "NSE circular NSE/CML/2025/00 (a test citation)"
    with app_connection(fresh_database_url) as conn:
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=START, end=END)

        rebuild_instruments(conn, manual_links=[ManualLink("ZOMATO", "ETERNAL", RENAMED, citation)])

        (link,) = queries.instrument_history(conn, ETERNAL).links
        (run,) = queries.ingest_runs(conn, source="instrument_links")
    assert (link.kind, link.on, link.from_symbol, link.to_symbol, link.source) == (
        "manual",
        RENAMED,
        "ZOMATO",
        "ETERNAL",
        f"manual link ZOMATO to ETERNAL effective 2025-04-09: {citation}",
    )
    assert run.details is not None
    assert (run.details["manual_links"], run.details["links"]) == (1, {"manual": 1})
