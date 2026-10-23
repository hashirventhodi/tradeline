"""``universe`` against recorded NSE end-of-day fixtures, asserted through the query layer.

The fixtures (``fixtures/universe/``) are the real NSE files for 2024-07-01 to
2024-08-02 trimmed to eight symbols: RELIANCE, INFY, TCS, and BAJAJ-AUTO,
listed for decades; ABDL, listed 2 July 2024; EMCURE, listed 10 July 2024;
NIFTYBEES, an ETF that trades in series EQ; and BAFNAPH, in series BE.

The rule's windows are shrunk from 126 trading days to 20 so a month of files
exercises them. The Universe for August 2024 is computed from the 20 trading
days before 1 August, 3 to 31 July (22 trading days in July less the first
two). The expected traded values were summed independently from the full,
untrimmed files with SQL over the loaded rows.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from playback import Playback, bhavcopy_series
from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.transport import PlaybackTransport
from tradeline.universe import Exclusion, compute_universe, standings

FIXTURES = Path(__file__).parent / "fixtures" / "universe"
AUGUST = date(2024, 8, 1)

RULE = UniverseRule(
    top_n=6,
    lookback_trading_days=20,
    min_listing_trading_days=21,
    min_presence_percent=90,
)

# Window traded value over 3 to 31 July 2024, from the untrimmed files.
RELIANCE = Decimal("331214637038.30")
INFY = Decimal("306766862236.30")
TCS = Decimal("270780964434.60")
BAJAJ_AUTO = Decimal("74544263106.95")
ABDL = Decimal("16526439524.75")


@pytest.fixture(scope="module")
def july_loaded(database_url: str) -> None:
    """End-of-day files up to the last trading day before August, and no later."""
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(FIXTURES), start=date(2024, 7, 1), end=date(2024, 7, 31))


@pytest.fixture
def conn(database_url: str, july_loaded: None) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def members(conn: psycopg.Connection, rule: UniverseRule = RULE) -> list[tuple[int, str, Decimal]]:
    return [
        (m.rank, m.symbol, m.traded_value)
        for m in queries.universe_membership(conn, AUGUST, rule_version=rule.version, as_of=AUGUST)
    ]


def test_the_universe_for_a_month_ranks_eligible_eq_symbols_by_window_traded_value(
    conn: psycopg.Connection,
) -> None:
    compute_universe(conn, RULE, month=AUGUST)

    # EMCURE is excluded on listing age and rows, NIFTYBEES as an ETF, and
    # BAFNAPH as series BE, so six places hold five members.
    assert members(conn) == [
        (1, "RELIANCE", RELIANCE),
        (2, "INFY", INFY),
        (3, "TCS", TCS),
        (4, "BAJAJ-AUTO", BAJAJ_AUTO),
        (5, "ABDL", ABDL),
    ]


def dump(conn: psycopg.Connection) -> bytes:
    """The membership table for the month as bytes: every column, numerics as stored."""
    return repr(
        queries.universe_membership(conn, AUGUST, rule_version=RULE.version, as_of=AUGUST)
    ).encode()


def test_running_it_twice_produces_byte_identical_membership(
    conn: psycopg.Connection, another_database_url: str
) -> None:
    first = compute_universe(conn, RULE, month=AUGUST)
    before = dump(conn)
    second = compute_universe(conn, RULE, month=AUGUST)

    assert second.rows_written == 0
    assert not second.anomaly
    assert second.members == first.members
    assert dump(conn) == before
    # And a separate database loaded from the same files computes the same bytes.
    with app_connection(another_database_url) as other:
        ingest_eod(
            other, PlaybackTransport(FIXTURES), start=date(2024, 7, 1), end=date(2024, 7, 31)
        )
        compute_universe(other, RULE, month=AUGUST)
        assert dump(other) == before


def test_end_of_day_rows_dated_on_the_first_trading_day_do_not_affect_membership(
    another_database_url: str,
) -> None:
    with app_connection(another_database_url) as other:
        ingest_eod(
            other, PlaybackTransport(FIXTURES), start=date(2024, 7, 1), end=date(2024, 7, 31)
        )
        before = compute_universe(other, RULE, month=AUGUST)
        # 1 August is August's first trading day; 2 August follows it.
        ingest_eod(other, PlaybackTransport(FIXTURES), start=AUGUST, end=date(2024, 8, 2))
        after = compute_universe(other, RULE, month=AUGUST)

        assert len(queries.end_of_day(other, "INFY", start=AUGUST, end=date(2024, 8, 2))) == 2
        assert not after.anomaly
        assert after.members == before.members
        assert (after.window_start, after.window_end) == (date(2024, 7, 3), date(2024, 7, 31))
        assert members(other) == [
            (1, "RELIANCE", RELIANCE),
            (2, "INFY", INFY),
            (3, "TCS", TCS),
            (4, "BAJAJ-AUTO", BAJAJ_AUTO),
            (5, "ABDL", ABDL),
        ]


def test_a_symbol_listed_fewer_trading_days_than_the_minimum_is_excluded(
    conn: psycopg.Connection,
) -> None:
    # ABDL listed on 2 July 2024, 21 trading days before August, and has a row
    # on every day of the window: only its listing age can exclude it.
    stricter = replace(RULE, min_listing_trading_days=22)

    compute_universe(conn, stricter, month=AUGUST)

    assert [symbol for _, symbol, _ in members(conn, stricter)] == [
        "RELIANCE",
        "INFY",
        "TCS",
        "BAJAJ-AUTO",
    ]


def test_a_symbol_with_rows_on_too_few_of_the_window_days_is_excluded(
    conn: psycopg.Connection,
) -> None:
    # EMCURE listed on 10 July 2024: 15 trading days, enough for this rule's
    # listing age, but 15 rows in a 20-day window where 90 percent needs 18.
    younger = replace(RULE, min_listing_trading_days=10)
    looser = replace(younger, min_presence_percent=75)

    compute_universe(conn, younger, month=AUGUST)
    compute_universe(conn, looser, month=AUGUST)

    assert "EMCURE" not in [symbol for _, symbol, _ in members(conn, younger)]
    assert (5, "EMCURE", Decimal("26254395753.00")) in members(conn, looser)


def test_standings_give_each_ineligible_symbol_the_first_filter_it_fails(
    conn: psycopg.Connection,
) -> None:
    # EMCURE fails Listing Age (15 trading days) and presence (15 of 20 rows);
    # Listing Age comes first in the rule's order. NIFTYBEES is a fund unit.
    excluded = {s.symbol: s.excluded for s in standings(conn, RULE, month=AUGUST)}

    assert excluded["EMCURE"] == Exclusion("listing_age", "listed 15 trading days, fewer than 21")
    assert str(excluded["EMCURE"]) == "listed 15 trading days, fewer than 21"
    assert excluded["NIFTYBEES"] is not None
    assert excluded["NIFTYBEES"].reason == "fund_unit"
    assert str(excluded["NIFTYBEES"]).endswith("is a fund unit (ETF)")
    assert excluded["RELIANCE"] is None


def test_standings_give_presence_once_listing_age_is_met(conn: psycopg.Connection) -> None:
    # EMCURE's 15 trading days meet a minimum of 10; its 15 rows of 20 do not meet 90%.
    younger = replace(RULE, min_listing_trading_days=10)

    excluded = {s.symbol: s.excluded for s in standings(conn, younger, month=AUGUST)}

    assert excluded["EMCURE"] == Exclusion("presence", "rows on 15 of 20 window days, under 90%")


def test_standings_give_series_for_a_symbol_last_traded_outside_it(
    fresh_database_url: str,
) -> None:
    # ABDL's row on 31 July, the window's last day, moved to series BE.
    files = Playback(FIXTURES, edit=bhavcopy_series(date(2024, 7, 31), "ABDL", "BE"))
    with app_connection(fresh_database_url) as conn:
        ingest_eod(conn, files, start=date(2024, 7, 1), end=date(2024, 7, 31))

        excluded = {s.symbol: s.excluded for s in standings(conn, RULE, month=AUGUST)}

    assert excluded["ABDL"] == Exclusion("series", "last traded in series BE in the window")


def test_changing_a_rule_parameter_records_a_new_rule_version_that_membership_references(
    conn: psycopg.Connection,
) -> None:
    top_three = replace(RULE, top_n=3)

    compute_universe(conn, RULE, month=AUGUST)
    result = compute_universe(conn, top_three, month=AUGUST)

    versions = {v.version: v.parameters for v in queries.universe_rule_versions(conn)}
    assert top_three.version != RULE.version
    assert result.rule_version == top_three.version
    assert versions[top_three.version]["top_n"] == 3
    assert versions[RULE.version]["top_n"] == 6
    assert members(conn, top_three) == [
        (1, "RELIANCE", RELIANCE),
        (2, "INFY", INFY),
        (3, "TCS", TCS),
    ]
    assert {
        m.rule_version
        for m in queries.universe_membership(
            conn, AUGUST, rule_version=top_three.version, as_of=AUGUST
        )
    } == {top_three.version}
    # The stored membership under the first version is untouched.
    assert len(members(conn, RULE)) == 5


def test_a_month_recomputed_differently_after_a_late_file_is_an_anomaly_and_stored_rows_kept(
    another_database_url: str,
) -> None:
    with app_connection(another_database_url) as other:
        ingest_eod(
            other, PlaybackTransport(FIXTURES), start=date(2024, 7, 1), end=date(2024, 7, 30)
        )
        early = compute_universe(other, RULE, month=AUGUST)
        ingest_eod(
            other, PlaybackTransport(FIXTURES), start=date(2024, 7, 31), end=date(2024, 7, 31)
        )
        late = compute_universe(other, RULE, month=AUGUST)
        stored = queries.universe_membership(other, AUGUST, rule_version=RULE.version, as_of=AUGUST)

    assert (early.window_start, early.window_end) == (date(2024, 7, 2), date(2024, 7, 30))
    assert late.anomaly
    assert late.rows_written == 0
    assert late.members != early.members
    assert tuple(stored) == early.members


def test_without_a_rule_version_the_latest_version_recorded_by_the_as_of_date_is_returned(
    conn: psycopg.Connection,
) -> None:
    compute_universe(conn, RULE, month=AUGUST)
    newest = replace(RULE, top_n=2)
    compute_universe(conn, newest, month=AUGUST)
    today = datetime.now(IST).date()

    latest = queries.universe_membership(conn, AUGUST, as_of=today)

    assert [(m.rule_version, m.symbol) for m in latest] == [
        (newest.version, "RELIANCE"),
        (newest.version, "INFY"),
    ]
    assert queries.universe_membership(conn, date(2024, 9, 1), as_of=today) == []
    # Both versions were recorded today, long after August 2024: on 1 August
    # 2024 no rule version was known, so there was no Universe to pick.
    assert queries.universe_membership(conn, AUGUST, as_of=AUGUST) == []


def test_the_universe_for_a_month_is_not_known_before_the_month(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="not known"):
        queries.universe_membership(conn, AUGUST, as_of=date(2024, 7, 31))


def test_a_month_must_be_given_by_its_first_day(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="first day"):
        compute_universe(conn, RULE, month=date(2024, 8, 2))


def test_a_month_without_enough_history_before_it_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="needs 20 trading days"):
        compute_universe(conn, RULE, month=date(2024, 7, 1))


def test_a_month_whose_end_of_day_files_are_not_loaded_up_to_its_start_is_refused(
    conn: psycopg.Connection,
) -> None:
    with pytest.raises(ValueError, match="latest end-of-day file"):
        compute_universe(conn, RULE, month=date(2024, 9, 1))
