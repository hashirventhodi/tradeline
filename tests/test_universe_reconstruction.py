"""Universe reconstruction (ADR 0005, 2026-10-24 amendment) against the recorded Universe
fixtures, asserted through the query layer.

The fixtures (``fixtures/universe/``, see ``test_universe.py``) are real NSE
files for 2024-07-01 to 2024-08-02 trimmed to eight symbols. The rule's window
is shrunk to 20 trading days, so August 2024 is built from 3 to 31 July: its
Data Cutoff is 31 July, the last trading day before 1 August.

Rule Validity declarations take the time they are declared at, so a test can
declare one before August 2024 began. Two tests stand in for history this
ticket cannot replay: one alters a stored row with the owner's connection, as
a bypass of the append-only grants would, and one stores a month the way
Phase 1 did, with no build row, and drops an ingest run's record.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.transport import PlaybackTransport
from tradeline.universe import (
    backfill_universe_builds,
    compute_universe,
    declare_rule_validity,
)

FIXTURES = Path(__file__).parent / "fixtures" / "universe"
AUGUST = date(2024, 8, 1)
JULY_31 = date(2024, 7, 31)

RULE = UniverseRule(
    top_n=6,
    lookback_trading_days=20,
    min_listing_trading_days=21,
    min_presence_percent=90,
)


def ist(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, tzinfo=IST)


def load(conn: psycopg.Connection, start: date = date(2024, 7, 1), end: date = JULY_31) -> None:
    ingest_eod(conn, PlaybackTransport(FIXTURES), start=start, end=end)


@pytest.fixture
def conn(fresh_database_url: str) -> Iterator[psycopg.Connection]:
    with app_connection(fresh_database_url) as conn:
        load(conn)
        yield conn


def eod_run_ids(conn: psycopg.Connection) -> tuple[object, ...]:
    return tuple(
        r.id for r in reversed(queries.ingest_runs(conn, source="nse_eod")) if r.rows_written
    )


def test_a_universe_run_records_the_month_build_with_its_cutoff_run_ids_and_hashes(
    conn: psycopg.Connection,
) -> None:
    result = compute_universe(conn, RULE, month=AUGUST)

    [build] = queries.universe_month_builds(conn, month=AUGUST, rule_version=RULE.version)
    assert result.build == build
    assert result.build_written
    assert (build.window_start, build.data_cutoff) == (date(2024, 7, 3), JULY_31)
    assert build.eod_run_ids == eod_run_ids(conn)
    assert len(build.eod_run_ids) == 1
    # No Instruments rebuild has run.
    assert build.instruments_run_id is None
    assert not build.cutoff_inferred
    assert len(build.eod_hash) == len(build.links_hash) == 64
    assert queries.universe_month_hash(conn, build).matches


def test_a_second_run_under_the_same_version_writes_no_build_and_reports_no_difference(
    conn: psycopg.Connection,
) -> None:
    first = compute_universe(conn, RULE, month=AUGUST)
    second = compute_universe(conn, RULE, month=AUGUST)

    assert not second.build_written
    assert second.build_differences == ()
    assert second.build == first.build
    assert len(queries.universe_month_builds(conn)) == 1


def test_a_file_loaded_after_a_build_changes_its_inputs_and_is_reported_not_recorded(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load(conn, end=date(2024, 7, 30))
        first = compute_universe(conn, RULE, month=AUGUST)
        load(conn, start=JULY_31, end=JULY_31)
        late = compute_universe(conn, RULE, month=AUGUST)

        assert not late.build_written
        assert set(late.build_differences) == {
            "window_start",
            "data_cutoff",
            "eod_run_ids",
            "eod_hash",
        }
        assert queries.universe_month_builds(conn) == [first.build]


def test_the_hash_of_a_month_changes_with_a_row_in_its_window_and_not_with_one_outside(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load(conn, end=date(2024, 8, 2))
        build = compute_universe(conn, RULE, month=AUGUST).build
        assert build is not None

        with psycopg.connect(fresh_database_url) as owner:
            # 1 August is August's first trading day, outside its window.
            owner.execute(
                "UPDATE eod_bhavcopy SET close = close + 1"
                " WHERE trade_date = '2024-08-01' AND symbol = 'INFY'"
            )
        outside = queries.universe_month_hash(conn, build)

        with psycopg.connect(fresh_database_url) as owner:
            owner.execute(
                "UPDATE eod_bhavcopy SET close = close + 1"
                " WHERE trade_date = '2024-07-15' AND symbol = 'INFY'"
            )
        inside = queries.universe_month_hash(conn, build)

    assert outside.matches
    assert outside.eod_hash == build.eod_hash
    assert not inside.matches
    assert inside.eod_hash != build.eod_hash
    assert inside.links_hash == build.links_hash


def test_the_build_names_the_instruments_rebuild_and_hashes_the_intervals_in_its_window(
    fresh_database_url: str,
) -> None:
    instruments = replace(RULE, rank_by="instrument")
    with app_connection(fresh_database_url) as conn:
        load(conn)
        before = queries.universe_window_hashes(conn, start=date(2024, 7, 3), cutoff=JULY_31)
        rebuild_instruments(conn)
        [rebuild] = queries.ingest_runs(conn, source="instrument_links")
        build = compute_universe(conn, instruments, month=AUGUST).build
        assert build is not None

        with psycopg.connect(fresh_database_url) as owner:
            owner.execute("UPDATE instrument_intervals SET isin = 'X' WHERE symbol = 'INFY'")
        after = queries.universe_month_hash(conn, build)

    assert build.instruments_run_id == rebuild.id
    assert build.eod_hash == before[0]
    assert build.links_hash != before[1]
    assert not after.matches
    assert after.eod_hash == build.eod_hash


def test_a_month_is_reconstructed_when_its_version_was_declared_in_force_only_later(
    conn: psycopg.Connection,
) -> None:
    compute_universe(conn, RULE, month=AUGUST)
    declare_rule_validity(
        conn, RULE.version, in_force_from=date(2024, 1, 1), declared_at=datetime.now(IST)
    )

    assert queries.universe_month_mode(conn, AUGUST, rule_version=RULE.version) == ("reconstructed")
    [validity] = queries.universe_rule_validity(conn)
    assert (validity.rule_version, validity.in_force_from) == (RULE.version, date(2024, 1, 1))


def test_a_month_is_recorded_when_its_version_was_in_force_and_recorded_before_it_began(
    conn: psycopg.Connection,
) -> None:
    declare_rule_validity(
        conn,
        RULE.version,
        in_force_from=date(2024, 7, 1),
        declared_at=ist(2024, 6, 15),
        rule=RULE,
    )
    compute_universe(conn, RULE, month=AUGUST)

    assert queries.universe_month_mode(conn, AUGUST, rule_version=RULE.version) == "recorded"


def test_a_month_before_its_version_came_into_force_is_reconstructed(
    conn: psycopg.Connection,
) -> None:
    declare_rule_validity(
        conn,
        RULE.version,
        in_force_from=date(2024, 9, 1),
        declared_at=ist(2024, 6, 15),
        rule=RULE,
    )
    compute_universe(conn, RULE, month=AUGUST)

    assert queries.universe_month_mode(conn, AUGUST, rule_version=RULE.version) == ("reconstructed")


def test_a_version_superseded_before_the_month_began_is_not_the_one_in_force(
    conn: psycopg.Connection,
) -> None:
    newer = replace(RULE, top_n=3)
    declare_rule_validity(
        conn, RULE.version, in_force_from=date(2024, 1, 1), declared_at=ist(2024, 1, 1), rule=RULE
    )
    declare_rule_validity(
        conn,
        newer.version,
        in_force_from=date(2024, 8, 1),
        declared_at=ist(2024, 7, 20),
        rule=newer,
    )
    compute_universe(conn, RULE, month=AUGUST)
    compute_universe(conn, newer, month=AUGUST)

    assert queries.universe_month_mode(conn, AUGUST, rule_version=RULE.version) == ("reconstructed")
    assert queries.universe_month_mode(conn, AUGUST, rule_version=newer.version) == "recorded"
    assert queries.universe_month_mode(conn, date(2024, 7, 1), rule_version=RULE.version) == (
        "recorded"
    )


def test_a_version_never_recorded_cannot_be_declared_in_force(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="not recorded"):
        declare_rule_validity(
            conn, RULE.version, in_force_from=date(2024, 1, 1), declared_at=datetime.now(IST)
        )


def test_an_unversioned_query_for_a_month_with_no_valid_version_stays_empty(
    conn: psycopg.Connection,
) -> None:
    compute_universe(conn, RULE, month=AUGUST)
    # Declared today, back to January: on 1 August 2024 no version was known.
    declare_rule_validity(
        conn, RULE.version, in_force_from=date(2024, 1, 1), declared_at=datetime.now(IST)
    )

    assert queries.universe_membership(conn, AUGUST, as_of=AUGUST) == []


SHORT = replace(RULE, lookback_trading_days=10)


def store_as_phase_1(url: str, rule: UniverseRule) -> None:
    """A month stored before builds were recorded: a rule version and one member, no build."""
    with psycopg.connect(url) as owner:
        owner.execute(
            "INSERT INTO universe_rule_versions (version, parameters, recorded_at)"
            " VALUES (%s, %s, now()) ON CONFLICT DO NOTHING",
            (rule.version, psycopg.types.json.Jsonb(rule.parameters)),
        )
        owner.execute(
            "INSERT INTO universe_membership (month, rule_version, rank, symbol, series,"
            " traded_value) VALUES (%s, %s, 1, 'RELIANCE', 'EQ', 1)",
            (AUGUST, rule.version),
        )


def test_months_stored_before_builds_are_backfilled_from_ingest_history_or_cutoff_inferred(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load(conn, end=date(2024, 7, 16))
        load(conn, start=date(2024, 7, 17), end=JULY_31)
        # The record of the first load is lost, so no run in history covers 3 to 16
        # July, which only the 20-day window reads.
        with psycopg.connect(fresh_database_url) as owner:
            owner.execute(
                "DELETE FROM ingest_runs WHERE source = 'nse_eod' AND range_start = '2024-07-01'"
            )
        store_as_phase_1(fresh_database_url, RULE)
        store_as_phase_1(fresh_database_url, SHORT)

        report = backfill_universe_builds(conn)
        again = backfill_universe_builds(conn)
        builds = {b.rule_version: b for b in queries.universe_month_builds(conn)}
        assert all(queries.universe_month_hash(conn, b).matches for b in builds.values())

    assert report.from_history == ((AUGUST, SHORT.version),)
    assert report.cutoff_inferred == ((AUGUST, RULE.version),)
    assert report.summary()["cutoff_inferred"] == [f"2024-08 {RULE.version}"]
    assert again.from_history == again.cutoff_inferred == ()
    assert builds[SHORT.version].window_start == date(2024, 7, 18)
    assert not builds[SHORT.version].cutoff_inferred
    assert builds[RULE.version].cutoff_inferred
    assert builds[RULE.version].data_cutoff == JULY_31
    assert builds[RULE.version].eod_run_ids == builds[SHORT.version].eod_run_ids


def test_a_universe_run_over_a_month_stored_without_a_build_leaves_it_to_the_backfill(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load(conn)
        store_as_phase_1(fresh_database_url, RULE)
        result = compute_universe(conn, RULE, month=AUGUST)

        assert result.build is None
        assert not result.build_written
        assert queries.universe_month_builds(conn) == []
