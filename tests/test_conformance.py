"""The Conformance Check (#18) against the ``fixtures/universe`` month, as in ``test_universe``.

The rule's windows are shrunk to 20 trading days, so the Universe for August
2024 is computed from 3 to 31 July. Five of the eight symbols are eligible:
RELIANCE, INFY, TCS, BAJAJ-AUTO and ABDL. EMCURE fails Listing Age and
presence, NIFTYBEES is a fund unit, and BAFNAPH trades in series BE, so it has
no EQ traded value to rank. A stored month the rule did not produce is seeded
under a rule version of its own, since membership rows are only ever added.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from playback import Playback, bhavcopy_rows
from tradeline.config import UniverseRule
from tradeline.conformance import check_conformance
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.transport import PlaybackTransport, Transport
from tradeline.universe import compute_universe

FIXTURES = Path(__file__).parent / "fixtures" / "universe"
AUGUST = date(2024, 8, 1)

RULE = UniverseRule(
    top_n=6,
    lookback_trading_days=20,
    min_listing_trading_days=21,
    min_presence_percent=90,
)

# Window traded value over 3 to 31 July 2024, from the untrimmed files (test_universe).
RELIANCE = Decimal("331214637038.30")
INFY = Decimal("306766862236.30")
TCS = Decimal("270780964434.60")
BAJAJ_AUTO = Decimal("74544263106.95")
ABDL = Decimal("16526439524.75")
EMCURE = Decimal("26254395753.00")


@pytest.fixture(scope="module")
def july_loaded(database_url: str) -> None:
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(FIXTURES), start=date(2024, 7, 1), end=date(2024, 7, 31))


@pytest.fixture
def conn(database_url: str, july_loaded: None) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def seed(
    url: str,
    rule: UniverseRule,
    members: list[tuple[int, str, Decimal]],
    *,
    files: Transport | None = None,
) -> None:
    """Load July's files and store ``members`` as the month's Universe under ``rule``."""
    with app_connection(url) as conn:
        ingest_eod(
            conn,
            files or PlaybackTransport(FIXTURES),
            start=date(2024, 7, 1),
            end=date(2024, 7, 31),
        )
        conn.execute(
            "INSERT INTO universe_rule_versions (version, parameters, recorded_at)"
            " VALUES (%s, %s, now())",
            (rule.version, Jsonb(rule.parameters)),
        )
        for rank, symbol, traded_value in members:
            conn.execute(
                "INSERT INTO universe_membership"
                " (month, rule_version, rank, symbol, series, traded_value)"
                " VALUES (%s, %s, %s, %s, 'EQ', %s)",
                (AUGUST, rule.version, rank, symbol, traded_value),
            )
        conn.commit()


def failures(url: str, rule: UniverseRule = RULE) -> tuple[str, ...]:
    with app_connection(url) as conn:
        report = check_conformance(conn, rule, months=[AUGUST])
    assert not report.passed
    return report.months[0].failures


def test_a_month_the_rule_computed_conforms(conn: psycopg.Connection) -> None:
    compute_universe(conn, RULE, month=AUGUST)

    report = check_conformance(conn, RULE, months=[AUGUST])

    (month,) = report.months
    assert report.passed
    assert month.month == AUGUST
    assert month.member_count == 5
    assert month.failures == ()
    # Five eligible for six places, so every non-member must have a reason.
    assert month.reasons() == {"listing_age": 1, "fund_unit": 1}
    assert {(a.key, str(a.excluded)) for a in month.excluded_above_the_cut} == {
        ("EMCURE", "listed 15 trading days, fewer than 21"),
        ("NIFTYBEES", "ISIN INF204KB14I2 is a fund unit (ETF)"),
    }


def test_a_non_member_above_the_cut_left_out_for_presence_is_reported_with_that_reason(
    conn: psycopg.Connection,
) -> None:
    # EMCURE meets this Listing Age minimum but has rows on 15 of the 20 window days.
    younger = replace(RULE, min_listing_trading_days=10)
    compute_universe(conn, younger, month=AUGUST)

    (month,) = check_conformance(conn, younger, months=[AUGUST]).months

    assert month.passed
    assert month.reasons() == {"presence": 1, "fund_unit": 1}


def test_a_member_whose_stored_traded_value_differs_from_the_recompute_fails(
    fresh_database_url: str,
) -> None:
    seed(
        fresh_database_url,
        RULE,
        [
            (1, "RELIANCE", RELIANCE),
            (2, "INFY", INFY),
            (3, "TCS", TCS + Decimal("0.01")),
            (4, "BAJAJ-AUTO", BAJAJ_AUTO),
            (5, "ABDL", ABDL),
        ],
    )

    assert failures(fresh_database_url) == (
        f"TCS: stored traded value {TCS + Decimal('0.01')}, recomputed {TCS}",
    )


def test_a_member_the_rule_does_not_admit_fails(fresh_database_url: str) -> None:
    # EMCURE has more traded value than ABDL, so the order holds; its Listing Age does not.
    seed(
        fresh_database_url,
        RULE,
        [
            (1, "RELIANCE", RELIANCE),
            (2, "INFY", INFY),
            (3, "TCS", TCS),
            (4, "BAJAJ-AUTO", BAJAJ_AUTO),
            (5, "EMCURE", EMCURE),
            (6, "ABDL", ABDL),
        ],
    )

    assert failures(fresh_database_url) == (
        "EMCURE is a member but not eligible: listed 15 trading days, fewer than 21",
    )


def test_members_out_of_traded_value_order_fail(fresh_database_url: str) -> None:
    seed(
        fresh_database_url,
        RULE,
        [
            (1, "RELIANCE", RELIANCE),
            (2, "TCS", TCS),
            (3, "INFY", INFY),
            (4, "BAJAJ-AUTO", BAJAJ_AUTO),
            (5, "ABDL", ABDL),
        ],
    )

    assert failures(fresh_database_url) == (
        "TCS ranks 2 above INFY, which has more traded value or wins the tie-break",
    )


def test_a_non_member_with_more_traded_value_and_no_reason_fails(fresh_database_url: str) -> None:
    seed(
        fresh_database_url,
        RULE,
        [
            (1, "RELIANCE", RELIANCE),
            (2, "TCS", TCS),
            (3, "BAJAJ-AUTO", BAJAJ_AUTO),
            (4, "ABDL", ABDL),
        ],
    )

    assert failures(fresh_database_url) == (
        f"INFY has {INFY} traded value, more than the last member ABDL,"
        " and the rule gives no reason to leave it out",
    )


def test_a_month_with_no_stored_universe_fails(conn: psycopg.Connection) -> None:
    report = check_conformance(conn, replace(RULE, top_n=7), months=[AUGUST])

    assert report.months[0].failures == ("no Universe is stored for the month",)


# ABDL's traded value on 15 July raised so its window total equals BAJAJ-AUTO's.
TIED = "298722578.05", str(Decimal("298722578.05") + BAJAJ_AUTO - ABDL)
FOUR = replace(RULE, top_n=4)


def tied(day: date, rows: list[str]) -> list[str]:
    if day != date(2024, 7, 15):
        return rows
    return [r.replace(TIED[0], TIED[1]) if ",ABDL," in r else r for r in rows]


def test_a_tie_at_the_cut_is_reported_and_broken_by_symbol(fresh_database_url: str) -> None:
    with app_connection(fresh_database_url) as conn:
        ingest_eod(
            conn,
            Playback(FIXTURES, edit=bhavcopy_rows(tied)),
            start=date(2024, 7, 1),
            end=date(2024, 7, 31),
        )
        compute_universe(conn, FOUR, month=AUGUST)

        (month,) = check_conformance(conn, FOUR, months=[AUGUST]).months

    # ABDL comes before BAJAJ-AUTO by Symbol, so ABDL takes fourth place.
    assert month.passed
    assert month.ties == ("BAJAJ-AUTO",)


def test_a_tie_at_the_cut_broken_against_the_symbol_order_fails(fresh_database_url: str) -> None:
    seed(
        fresh_database_url,
        FOUR,
        [
            (1, "RELIANCE", RELIANCE),
            (2, "INFY", INFY),
            (3, "TCS", TCS),
            (4, "BAJAJ-AUTO", BAJAJ_AUTO),
        ],
        files=Playback(FIXTURES, edit=bhavcopy_rows(tied)),
    )
    with app_connection(fresh_database_url) as conn:
        (month,) = check_conformance(conn, FOUR, months=[AUGUST]).months

    assert month.ties == ("ABDL",)
    assert month.failures == (
        "ABDL ties the last member BAJAJ-AUTO and wins the tie-break by Symbol, but is left out",
    )
