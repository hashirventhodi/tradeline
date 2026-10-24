"""The Ledger's tables hold what ADR 0019 says, whatever writes to them: a backtest id exactly
when the mode is backtest, and no row changed or removed by the application role, whether
through a table or one of its mode partitions."""

import uuid

import psycopg
import pytest

from tradeline.db import app_connection

LEDGER_TABLES = ("strategies", "runs", "candidates", "plans")
INSERTS = {
    "strategies": "INSERT INTO strategies (mode, backtest_id, family, name, code_revision,"
    " ranking) VALUES (%s, %s, 'pullback', 'pullback_v1', 'r', 'by return')",
    "runs": "INSERT INTO runs (mode, backtest_id, strategy_id, as_of, universe_month,"
    " universe_rule_version, universe_size, parameters)"
    " VALUES (%s, %s, gen_random_uuid(), '2023-06-15', '2023-06-01', 'v', 5, '{}')",
    "candidates": "INSERT INTO candidates (mode, backtest_id, run_id, rank, instrument, symbol,"
    " score, facts) VALUES (%s, %s, gen_random_uuid(), 1, 'A', 'A', 0.1, '{}')",
    "plans": "INSERT INTO plans (mode, backtest_id, run_id, rank, instrument, symbol, entry,"
    " stop, target, time_stop_days, facts)"
    " VALUES (%s, %s, gen_random_uuid(), 1, 'A', 'A', 100, 94, 112, 10, '{}')",
}


@pytest.mark.parametrize("table", LEDGER_TABLES)
@pytest.mark.parametrize(
    ("mode", "backtest_id"),
    [("paper", uuid.uuid4()), ("live", uuid.uuid4()), ("backtest", None)],
    ids=["paper-with-id", "live-with-id", "backtest-without-id"],
)
def test_a_ledger_row_needs_a_backtest_id_exactly_when_its_mode_is_backtest(
    database_url: str, table: str, mode: str, backtest_id: uuid.UUID | None
) -> None:
    with app_connection(database_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(INSERTS[table], (mode, backtest_id))


def test_a_strategy_row_in_either_shape_is_accepted(database_url: str) -> None:
    with app_connection(database_url) as conn:
        conn.execute(INSERTS["strategies"], ("backtest", uuid.uuid4()))
        conn.execute(INSERTS["strategies"], ("paper", None))
        conn.rollback()


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_the_application_role_cannot_update_or_delete_ledger_rows_in_any_partition(
    database_url: str, table: str
) -> None:
    with app_connection(database_url) as conn:
        for name in (table, f"{table}_backtest", f"{table}_paper", f"{table}_live"):
            for statement in ("UPDATE {} SET mode = mode", "DELETE FROM {}", "TRUNCATE {}"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(psycopg.sql.SQL(statement).format(psycopg.sql.Identifier(name)))
                conn.rollback()
