import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg
import pytest

from tradeline.db import app_connection, connect
from tradeline.migrations import applied_migrations, apply_migrations


def test_migrate_creates_the_schema_from_empty_and_is_a_no_op_second_time(
    empty_database_url: str,
) -> None:
    with connect(empty_database_url) as conn:
        assert applied_migrations(conn) == []

        first = apply_migrations(conn)
        second = apply_migrations(conn)

        assert first and first[0] == "0001_ingest_runs"
        assert second == []
        assert applied_migrations(conn) == first


def test_concurrent_first_runs_of_migrate_apply_each_migration_exactly_once(
    empty_database_url: str,
) -> None:
    def run() -> list[str]:
        with connect(empty_database_url) as conn:
            return apply_migrations(conn)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run(), range(4)))

    applied = [version for result in results for version in result]
    assert sorted(applied) == sorted(set(applied))
    with connect(empty_database_url) as conn:
        assert applied_migrations(conn) == sorted(applied)


def test_the_application_role_can_insert_an_ingest_run(database_url: str) -> None:
    run_id = uuid.uuid4()
    with app_connection(database_url) as conn:
        conn.execute(
            "INSERT INTO ingest_runs (id, command, source, started_at, finished_at, rows_written)"
            " VALUES (%s, 'ingest', 'kite', %s, %s, 10)",
            (run_id, datetime.now(UTC), datetime.now(UTC)),
        )
        conn.commit()
        row = conn.execute("SELECT source FROM ingest_runs WHERE id = %s", (run_id,)).fetchone()

    assert row == ("kite",)


def test_the_application_role_cannot_update_or_delete_ingest_runs(database_url: str) -> None:
    with app_connection(database_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE ingest_runs SET rows_written = 0")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM ingest_runs")
        conn.rollback()


def test_the_application_role_cannot_update_or_delete_end_of_day_rows(database_url: str) -> None:
    with app_connection(database_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE eod_bhavcopy SET close = 0")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM eod_bhavcopy")
        conn.rollback()


def test_the_application_role_cannot_update_or_delete_daily_candles(database_url: str) -> None:
    with app_connection(database_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE candles_daily SET close = 0")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM candles_daily")
        conn.rollback()


def test_the_application_role_cannot_update_or_delete_corporate_actions(
    database_url: str,
) -> None:
    with app_connection(database_url) as conn:
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator)"
            " VALUES ('ROLE', 'EQ', '2024-07-05', 'Bonus 1:5', 'bonus', 5, 6)"
        )
        (factor,) = conn.execute(
            "SELECT factor FROM corporate_actions WHERE symbol = 'ROLE'"
        ).fetchone()  # type: ignore[misc]
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE corporate_actions SET kind = 'other'")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM corporate_actions")
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):  # a bonus must carry a factor
            conn.execute(
                "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind)"
                " VALUES ('ROLE', 'EQ', '2024-07-05', 'Bonus 1:1', 'bonus')"
            )
        conn.rollback()

    assert str(factor).startswith("0.8333333")
