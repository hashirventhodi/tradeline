"""``verify-universe``: the current Universe against NSE's current NIFTY 200, as a report.

The Universe is computed from the eight-symbol end-of-day fixtures of
``test_universe`` (August 2024, the rule's windows shrunk to 20 trading days).
The NIFTY 200 list and the sector list are NSE's, recorded on 2026-10-21 and
trimmed (``fixtures/sectors/``): the NIFTY 200 to BAJAJ-AUTO, HDFCBANK, INFY,
RELIANCE, and TCS. HDFCBANK has no rows in the end-of-day fixtures, so it is
the constituent the rule cannot rank. A symbol without a sector is made by
playing the real recorded sector list back through ``omit``, which drops one
symbol's line on its way to the service.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from playback import Playback, lines
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.sectors import load_sectors
from tradeline.transport import PlaybackTransport
from tradeline.universe import compute_universe
from tradeline.verify_universe import verify_universe

FIXTURES = Path(__file__).parent / "fixtures"
SECTOR_FIXTURES = FIXTURES / "sectors"
AUGUST = date(2024, 8, 1)
SECTORS_LOADED = date(2024, 7, 31)

RULE = UniverseRule(
    top_n=6,
    lookback_trading_days=20,
    min_listing_trading_days=21,
    min_presence_percent=90,
)


def omit(symbol: str) -> Playback:
    """The recorded lists played back without ``symbol``'s line."""
    return Playback(
        SECTOR_FIXTURES,
        edit=lines(lambda rows: [row for row in rows if row.split(",")[2:3] != [symbol]]),
    )


@pytest.fixture(scope="module")
def loaded(database_url: str) -> None:
    with app_connection(database_url) as conn:
        ingest_eod(
            conn,
            PlaybackTransport(FIXTURES / "universe"),
            start=date(2024, 7, 1),
            end=date(2024, 7, 31),
        )
        compute_universe(conn, RULE, month=AUGUST)
        compute_universe(conn, replace(RULE, top_n=3), month=AUGUST)
        load_sectors(conn, PlaybackTransport(SECTOR_FIXTURES), effective_date=SECTORS_LOADED)


@pytest.fixture
def conn(database_url: str, loaded: None) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


@pytest.fixture
def other(fresh_database_url: str) -> Iterator[psycopg.Connection]:
    """A database of its own, for a test that loads a different sector list."""
    with app_connection(fresh_database_url) as conn:
        ingest_eod(
            conn,
            PlaybackTransport(FIXTURES / "universe"),
            start=date(2024, 7, 1),
            end=date(2024, 7, 31),
        )
        compute_universe(conn, RULE, month=AUGUST)
        yield conn


def test_the_report_prints_overlap_and_both_difference_lists_in_a_stable_format(
    conn: psycopg.Connection,
) -> None:
    check = verify_universe(
        conn, PlaybackTransport(SECTOR_FIXTURES), RULE, month=AUGUST, as_of=AUGUST
    )

    assert check.text() == (
        f"Universe 2024-08 (rule {RULE.version[:12]}) against NIFTY 200 as of 2024-08-01\n"
        "Overlap: 4 of 5 NIFTY 200 constituents (80.00%)\n"
        "\n"
        "In the Universe, not in NIFTY 200: 1\n"
        "   rank  symbol               traded value  note\n"
        "      5  ABDL               16526439524.75\n"
        "\n"
        "In NIFTY 200, not in the Universe: 1\n"
        "   rank  symbol               traded value  note\n"
        "      -  HDFCBANK                        -  no EQ rows in the window\n"
        "\n"
        "Universe members without a sector: 0\n"
    )
    # The same data reports the same bytes.
    again = verify_universe(
        conn, PlaybackTransport(SECTOR_FIXTURES), RULE, month=AUGUST, as_of=AUGUST
    )
    assert again.text() == check.text()


def test_a_constituent_outside_the_universe_shows_where_the_rule_ranks_it(
    conn: psycopg.Connection,
) -> None:
    top_three = replace(RULE, top_n=3)

    check = verify_universe(
        conn, PlaybackTransport(SECTOR_FIXTURES), top_three, month=AUGUST, as_of=AUGUST
    )

    assert (check.overlap, check.index_size) == (3, 5)
    assert check.overlap_percent == Decimal("60.00")
    assert check.universe_only == ()
    assert [(s.symbol, s.rank, s.traded_value, s.note) for s in check.index_only] == [
        ("BAJAJ-AUTO", 4, Decimal("74544263106.95"), "ranked below the top 3"),
        ("HDFCBANK", None, None, "no EQ rows in the window"),
    ]


def test_universe_members_without_a_sector_are_listed(other: psycopg.Connection) -> None:
    load_sectors(other, omit("ABDL"), effective_date=SECTORS_LOADED)

    check = verify_universe(
        other, PlaybackTransport(SECTOR_FIXTURES), RULE, month=AUGUST, as_of=AUGUST
    )

    assert [(m.rank, m.symbol) for m in check.without_sector] == [(5, "ABDL")]
    assert check.text().endswith(
        "Universe members without a sector: 1\n   rank  symbol\n      5  ABDL\n"
    )


def test_a_sector_loaded_after_the_as_of_date_does_not_count(other: psycopg.Connection) -> None:
    load_sectors(other, PlaybackTransport(SECTOR_FIXTURES), effective_date=date(2024, 8, 2))

    check = verify_universe(
        other, PlaybackTransport(SECTOR_FIXTURES), RULE, month=AUGUST, as_of=AUGUST
    )

    assert [m.symbol for m in check.without_sector] == [
        "RELIANCE",
        "INFY",
        "TCS",
        "BAJAJ-AUTO",
        "ABDL",
    ]


def test_a_month_the_rule_has_not_computed_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="tradeline universe --month 2024-09"):
        verify_universe(
            conn,
            PlaybackTransport(SECTOR_FIXTURES),
            RULE,
            month=date(2024, 9, 1),
            as_of=date(2024, 9, 2),
        )
