"""The Universe Rule ranking Instruments, against a rename inside the window.

The fixtures (``fixtures/universe_rename/``, see its README) are NSE's
end-of-day files for 17 March to 30 April 2025 trimmed to RELIANCE, INFY, TCS,
and ZOMATO, renamed ETERNAL on 9 April 2025, and NSE's equity master trimmed
to the four listed today. NSE's symbol-change list comes from
``fixtures/instruments/`` and Kite's instrument list from
``fixtures/ingest_eod/``.

The rule's windows are shrunk to 20 trading days, so the Universe for May 2025
is computed from 28 March to 30 April: ZOMATO trades on 7 of those days and
ETERNAL on the other 13. The expected traded values were summed independently
from the untrimmed files: series EQ, by Symbol, over the window.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from playback import Edit, Playback, lines
from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.sources.nse import EQUITY_MASTER_URL
from tradeline.transport import PlaybackTransport, Request
from tradeline.universe import compute_universe, standings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = FIXTURES_DIR / "universe_rename"
INSTRUMENT_FIXTURES = FIXTURES_DIR / "instruments"
EOD_FIXTURES = FIXTURES_DIR / "ingest_eod"
START, END = date(2025, 3, 17), date(2025, 4, 30)
MAY = date(2025, 5, 1)

BY_SYMBOL = UniverseRule(
    top_n=6,
    lookback_trading_days=20,
    min_listing_trading_days=21,
    min_presence_percent=90,
)
BY_INSTRUMENT = replace(BY_SYMBOL, rank_by="instrument")
EARLIEST = replace(BY_INSTRUMENT, listing_age_from="earliest")

# Window traded value over 28 March to 30 April 2025, from the untrimmed files.
RELIANCE = Decimal("402258396718.85")
INFY = Decimal("306233039628.80")
TCS = Decimal("223900130440.30")
ZOMATO = Decimal("86660937388.69")  # 28 March to 8 April
ETERNAL = Decimal("126470135172.75")  # 9 to 30 April


def load(url: str, *, master: Playback | None = None) -> None:
    with app_connection(url) as conn:
        ingest_eod(conn, master or Playback(FIXTURES, EOD_FIXTURES), start=START, end=END)
        ingest_symbol_changes(conn, PlaybackTransport(INSTRUMENT_FIXTURES))
        rebuild_instruments(conn)


@pytest.fixture(scope="module")
def loaded(database_url: str) -> None:
    load(database_url)


@pytest.fixture
def conn(database_url: str, loaded: None) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def members(conn: psycopg.Connection, rule: UniverseRule) -> list[tuple[int, str, Decimal]]:
    return [
        (m.rank, m.symbol, m.traded_value)
        for m in queries.universe_membership(conn, MAY, rule_version=rule.version, as_of=MAY)
    ]


def test_a_member_renamed_inside_the_window_keeps_its_place_with_its_whole_traded_value(
    conn: psycopg.Connection,
) -> None:
    compute_universe(conn, BY_SYMBOL, month=MAY)
    compute_universe(conn, BY_INSTRUMENT, month=MAY)

    # By Symbol, ZOMATO and ETERNAL each have rows on too few of the window's days.
    assert members(conn, BY_SYMBOL) == [
        (1, "RELIANCE", RELIANCE),
        (2, "INFY", INFY),
        (3, "TCS", TCS),
    ]
    assert members(conn, BY_INSTRUMENT) == [
        (1, "RELIANCE", RELIANCE),
        (2, "INFY", INFY),
        (3, "TCS", TCS),
        (4, "ETERNAL", ZOMATO + ETERNAL),
    ]


def test_membership_records_the_instrument_and_the_symbol_in_force_on_the_last_window_day(
    conn: psycopg.Connection,
) -> None:
    compute_universe(conn, BY_SYMBOL, month=MAY)
    compute_universe(conn, BY_INSTRUMENT, month=MAY)

    assert [
        (m.symbol, m.instrument)
        for m in queries.universe_membership(
            conn, MAY, rule_version=BY_INSTRUMENT.version, as_of=MAY
        )
    ] == [
        ("RELIANCE", "RELIANCE@1995-11-29"),
        ("INFY", "INFY@1995-02-08"),
        ("TCS", "TCS@2004-08-25"),
        ("ETERNAL", "ZOMATO@2021-07-23"),
    ]
    # A rule that ranks Symbols records no Instrument.
    assert {
        m.instrument
        for m in queries.universe_membership(conn, MAY, rule_version=BY_SYMBOL.version, as_of=MAY)
    } == {None}


def without_master_row(symbol: str) -> Edit:
    """NSE's equity master played back without ``symbol``'s row; other bodies as recorded."""
    drop = lines(lambda rows: [row for row in rows if not row.startswith(f"{symbol},")])

    def edit(request: Request, body: bytes) -> bytes:
        return drop(request, body) if request.url == EQUITY_MASTER_URL else body

    return edit


def test_a_renamed_member_is_not_excluded_as_recently_listed(fresh_database_url: str) -> None:
    # With no listing date from NSE, the new Symbol on its own dates from its
    # first row, 9 April: 13 trading days before May. The Instrument dates
    # from ZOMATO's first row, 17 March: 30.
    load(
        fresh_database_url,
        master=Playback(FIXTURES, EOD_FIXTURES, edit=without_master_row("ETERNAL")),
    )
    with app_connection(fresh_database_url) as conn:
        by_symbol = {s.symbol: s for s in standings(conn, BY_SYMBOL, month=MAY)}
        compute_universe(conn, BY_INSTRUMENT, month=MAY)

        assert by_symbol["ETERNAL"].excluded == "listed 13 trading days, fewer than 21"
        assert (4, "ETERNAL", ZOMATO + ETERNAL) in members(conn, BY_INSTRUMENT)
        assert queries.resolve_symbol(conn, "ETERNAL", as_of=END).key == "ZOMATO@2025-03-17"


def admitted_late(request: Request, body: bytes) -> bytes:
    """NSE's equity master with RELIANCE's listing date moved to 21 April 2025, after its
    first row, as the master dates a stock's move from "permitted to trade" to listed."""
    if request.url != EQUITY_MASTER_URL:
        return body
    readmit = lines(lambda rows: [row.replace("29-NOV-1995", "21-APR-2025") for row in rows])
    return readmit(request, body)


def test_listing_age_counts_from_the_first_row_when_the_listing_date_comes_after_it(
    fresh_database_url: str,
) -> None:
    # ADR 0017. From 21 April RELIANCE has 8 trading days before May; from its
    # first row, 17 March, 29 (the holidays are 31 March and 10, 14 and 18 April).
    load(fresh_database_url, master=Playback(FIXTURES, EOD_FIXTURES, edit=admitted_late))
    with app_connection(fresh_database_url) as conn:
        by_listing_date = {s.symbol: s for s in standings(conn, BY_INSTRUMENT, month=MAY)}
        compute_universe(conn, EARLIEST, month=MAY)

        assert by_listing_date["RELIANCE"].excluded == "listed 8 trading days, fewer than 21"
        assert (1, "RELIANCE", RELIANCE) in members(conn, EARLIEST)
        # The key keeps NSE's date.
        assert queries.resolve_symbol(conn, "RELIANCE", as_of=END).key == "RELIANCE@2025-04-21"


def dump(conn: psycopg.Connection, rule: UniverseRule) -> bytes:
    return repr(
        queries.universe_membership(conn, MAY, rule_version=rule.version, as_of=MAY)
    ).encode()


def test_ranking_instruments_leaves_the_symbol_versions_rows_and_reruns_byte_identically(
    conn: psycopg.Connection, another_database_url: str
) -> None:
    compute_universe(conn, BY_SYMBOL, month=MAY)
    by_symbol = dump(conn, BY_SYMBOL)

    first = compute_universe(conn, BY_INSTRUMENT, month=MAY)
    by_instrument = dump(conn, BY_INSTRUMENT)
    second = compute_universe(conn, BY_INSTRUMENT, month=MAY)

    assert (second.rows_written, second.anomaly, second.members) == (0, False, first.members)
    assert dump(conn, BY_INSTRUMENT) == by_instrument
    assert dump(conn, BY_SYMBOL) == by_symbol
    # And a separate database loaded from the same files computes the same bytes.
    load(another_database_url)
    with app_connection(another_database_url) as other:
        compute_universe(other, BY_INSTRUMENT, month=MAY)
        assert dump(other, BY_INSTRUMENT) == by_instrument


def test_ranking_instruments_is_refused_until_they_are_rebuilt_over_the_window(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=START, end=date(2025, 4, 29))
        ingest_symbol_changes(conn, PlaybackTransport(INSTRUMENT_FIXTURES))
        rebuild_instruments(conn)
        # 30 April's file arrives after the rebuild: its four rows belong to no Instrument.
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=END, end=END)

        with pytest.raises(ValueError, match="4 end-of-day rows .* rebuild-instruments"):
            compute_universe(conn, BY_INSTRUMENT, month=MAY)
        assert (
            queries.universe_membership(conn, MAY, rule_version=BY_INSTRUMENT.version, as_of=MAY)
            == []
        )

        rebuild_instruments(conn)
        assert len(compute_universe(conn, BY_INSTRUMENT, month=MAY).members) == 4
