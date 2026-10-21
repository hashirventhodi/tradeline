import uuid
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
