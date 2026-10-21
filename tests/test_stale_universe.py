"""Universe months a late Instrument Link made stale, reported and never recomputed.

The fixtures are ``test_universe_instruments``': NSE's end-of-day files for 17
March to 30 April 2025 trimmed to RELIANCE, INFY, TCS, and ZOMATO, renamed
ETERNAL on 9 April 2025. NSE's symbol-change list is played back without the
ZOMATO to ETERNAL record, so the first rebuild splits the history in two and
the Universe is stored over that split. The Operator then adds the rename as
a manual link, which arrives late.

The rules' windows are shrunk. Over 20 trading days, May 2025's window runs
from 28 March to 30 April, across the rename. Over 10, April 2025's runs from
17 to 28 March, before it.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import psycopg
import pytest

from playback import Playback, lines
from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.instruments import ManualLink
from tradeline.instruments_report import instruments_report
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.universe import compute_universe

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = FIXTURES_DIR / "universe_rename"
INSTRUMENT_FIXTURES = FIXTURES_DIR / "instruments"
EOD_FIXTURES = FIXTURES_DIR / "ingest_eod"
START, END = date(2025, 3, 17), date(2025, 4, 30)
APRIL, MAY = date(2025, 4, 1), date(2025, 5, 1)

BY_SYMBOL = UniverseRule(
    top_n=6,
    lookback_trading_days=20,
    min_listing_trading_days=1,
    min_presence_percent=60,
)
BY_INSTRUMENT = replace(BY_SYMBOL, rank_by="instrument")
SHORT = replace(BY_INSTRUMENT, lookback_trading_days=10)
RENAME = ManualLink(
    "ZOMATO",
    "ETERNAL",
    date(2025, 4, 9),
    "https://nsearchives.nseindia.com/content/equities/symbolchange.csv",
)


def without_zomato(rows: list[str]) -> list[str]:
    return [row for row in rows if ",ZOMATO,ETERNAL," not in row]


@pytest.fixture
def split(fresh_database_url: str) -> Iterator[psycopg.Connection]:
    """The Instruments built without the rename: ZOMATO and ETERNAL apart."""
    with app_connection(fresh_database_url) as conn:
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=START, end=END)
        ingest_symbol_changes(conn, Playback(INSTRUMENT_FIXTURES, edit=lines(without_zomato)))
        rebuild_instruments(conn)
        yield conn


Stored = list[tuple[UniverseRule, date]]


def store(conn: psycopg.Connection, months: Stored) -> list[queries.UniverseMember]:
    for rule, month in months:
        compute_universe(conn, rule, month=month)
    return membership(conn, months)


def membership(conn: psycopg.Connection, months: Stored) -> list[queries.UniverseMember]:
    return [
        member
        for rule, month in months
        for member in queries.universe_membership(
            conn, month, rule_version=rule.version, as_of=month
        )
    ]


def test_a_late_link_inside_a_stored_window_makes_that_month_stale(
    split: psycopg.Connection,
) -> None:
    months = [(BY_SYMBOL, MAY), (BY_INSTRUMENT, MAY)]
    before = store(split, months)
    assert instruments_report(split).stale == ()

    rebuild_instruments(split, manual_links=[RENAME])

    # The rule that ranks Symbols never reads the Instruments, so its May stands.
    report = instruments_report(split)
    assert [(s.rule_version, s.month) for s in report.stale] == [(BY_INSTRUMENT.version, MAY)]
    assert membership(split, months) == before


def test_a_rename_effective_after_every_stored_window_makes_nothing_stale(
    split: psycopg.Connection,
) -> None:
    store(split, [(SHORT, APRIL)])

    rebuild_instruments(split, manual_links=[RENAME])

    # Before 9 April ZOMATO's history is unbroken, joined or not; only its key changes.
    assert queries.resolve_symbol(split, "ZOMATO", as_of=date(2025, 3, 28)).key == (
        "ZOMATO@2021-07-23"
    )
    assert instruments_report(split).stale == ()


def test_a_stale_month_stays_stale_and_is_reported_by_rule_version(
    split: psycopg.Connection,
) -> None:
    store(split, [(SHORT, APRIL), (BY_INSTRUMENT, MAY)])

    rebuild_instruments(split, manual_links=[RENAME])
    # A later rebuild finds nothing new, and the month the first found stays stale.
    rebuild_instruments(split, manual_links=[RENAME])

    again, found = queries.ingest_runs(split, source="instrument_links")[:2]
    assert (found.details["stale_months"], again.details["stale_months"]) == (1, 0)
    report = instruments_report(split)
    assert report.summary()["stale_months"] == {BY_INSTRUMENT.version: ["2025-05"]}
    assert report.text().endswith(
        f"\nStale Universe months: 1\n  rule {BY_INSTRUMENT.version}: 2025-05\n"
    )
