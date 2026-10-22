import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from fractions import Fraction

import psycopg
import pytest

from tradeline.adjust import interpret
from tradeline.db import app_connection, connect
from tradeline.migrations import (
    SCHEMA_MIGRATIONS_READ,
    UnreadableMigrations,
    applied_migrations,
    apply_migrations,
)


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


def test_the_application_role_can_read_the_applied_migrations(database_url: str) -> None:
    # A login role that is only a member of tradeline_app (db.py) must still see
    # whether a migration is pending.
    with connect(database_url) as conn:
        applied = applied_migrations(conn)
    with app_connection(database_url) as conn:
        assert applied_migrations(conn) == applied


def test_the_application_role_cannot_record_a_migration(database_url: str) -> None:
    with (
        app_connection(database_url) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("INSERT INTO schema_migrations (version) VALUES ('9999_forged')")


@pytest.mark.parametrize("inherit", [True, False], ids=["inherit", "noinherit"])
def test_a_member_only_login_role_reads_the_applied_migrations_and_nothing_owner_only(
    database_url: str, member_only_url: Callable[..., str], inherit: bool
) -> None:
    member_url = member_only_url(database_url, inherit=inherit)
    with connect(database_url) as conn:
        applied = applied_migrations(conn)

    with app_connection(member_url) as conn:
        assert applied_migrations(conn) == applied
        for statement in (
            "INSERT INTO schema_migrations (version) VALUES ('9999_forged')",
            "DROP TABLE ingest_runs",
            "CREATE TABLE forged (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def _with_0022_pending(url: str) -> None:
    """As a database migrated before 0022 was merged: tradeline_app cannot read the table."""
    with connect(url) as conn:
        apply_migrations(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version = %s", (SCHEMA_MIGRATIONS_READ,))
        conn.execute("REVOKE SELECT ON schema_migrations FROM tradeline_app")


@pytest.mark.parametrize("inherit", [True, False], ids=["inherit", "noinherit"])
def test_a_member_only_login_role_is_told_to_migrate_while_0022_is_pending(
    empty_database_url: str, member_only_url: Callable[..., str], inherit: bool
) -> None:
    _with_0022_pending(empty_database_url)
    member_url = member_only_url(empty_database_url, inherit=inherit)

    with pytest.raises(UnreadableMigrations):
        app_connection(member_url)

    with connect(empty_database_url) as conn:
        apply_migrations(conn)
    with app_connection(member_url) as conn:
        assert applied_migrations(conn)


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


def test_the_application_role_cannot_update_or_delete_minute_candles_in_any_partition(
    database_url: str,
) -> None:
    with app_connection(database_url) as conn:
        (partition,) = conn.execute("SELECT candles_minute_partition('2024-07-01')").fetchone()  # type: ignore[misc]
        conn.commit()
        for table in ("candles_minute", partition):
            for statement in ("UPDATE {} SET close = 0", "DELETE FROM {}"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(psycopg.sql.SQL(statement).format(psycopg.sql.Identifier(table)))
                conn.rollback()


def test_the_application_role_cannot_update_or_delete_news_items(database_url: str) -> None:
    with app_connection(database_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE news_items SET symbols = '{}'")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM news_items")
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


def test_a_corporate_action_names_an_nse_list_or_carries_a_citation_never_both(
    database_url: str,
) -> None:
    insert = (
        "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
        " factor_numerator, factor_denominator, nse_list, citation)"
        " VALUES ('CITED', 'BE', '2020-07-02', %s, %s, %s, %s, %s, %s)"
    )
    split = ("Sub-Division from Rs. 10/- to Rs. 2/-", "split", 1, 5)
    with app_connection(database_url) as conn:
        conn.execute(insert, (*split, None, "NSE/CML/44835"))
        conn.rollback()
        for row in [
            (*split, None, None),  # neither
            (*split, "equities", "NSE/CML/44835"),  # both
            ("AGM", "other", None, None, None, "NSE/CML/44835"),  # a cited row that adjusts nothing
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, row)
            conn.rollback()


def test_the_application_role_cannot_update_or_delete_announcements(
    database_url: str,
) -> None:
    with app_connection(database_url) as conn:
        conn.execute(
            "INSERT INTO announcements (source, source_id, disseminated_at, exchange_code, subject)"
            " VALUES ('nse', 'ROLE', '2024-07-23 18:00+05:30', 'ROLE', 'Updates')"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE announcements SET symbol = 'ROLE'")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM announcements")
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):  # only the two exchanges
            conn.execute(
                "INSERT INTO announcements"
                " (source, source_id, disseminated_at, exchange_code, subject)"
                " VALUES ('mcx', 'ROLE', '2024-07-23 18:00+05:30', 'ROLE', 'Updates')"
            )
        conn.rollback()


def test_the_application_role_cannot_update_or_delete_universe_tables(database_url: str) -> None:
    statements = (
        "UPDATE universe_rule_versions SET recorded_at = now()",
        "DELETE FROM universe_rule_versions",
        "UPDATE universe_membership SET rank = rank + 1",
        "DELETE FROM universe_membership",
    )
    with app_connection(database_url) as conn:
        for statement in statements:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_the_application_role_cannot_update_or_delete_sectors(database_url: str) -> None:
    with app_connection(database_url) as conn:
        for statement in ("UPDATE sectors SET sector = 'Other'", "DELETE FROM sectors"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_the_application_role_cannot_update_or_delete_symbol_changes(database_url: str) -> None:
    with app_connection(database_url) as conn:
        for statement in (
            "UPDATE symbol_changes SET new_symbol = 'OTHER'",
            "DELETE FROM symbol_changes",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


# Every short form 0019 re-derives, read as ``interpret`` reads it: the 14 rows
# of the #13 database are of the first two shapes.
SHORT_FORMS = [
    "Fv Splt Frm Rs 10 To Re 1",
    "Fv Splt Frm Rs 10 To Rs 2",
    "Fv Splt Frm Rs 10 To Rs 5",
    "Fv Splt Frm Rs 5 To Re 1",
    "Fv Splt Frm Rs. 10 To Rs. 5",
    "Fv Splt Frm Rs 2.50 To Re 1",
    "FV SPLT FRM Rs10 To Re1",
    "Fv  Splt  Frm Rs 10/- To Rs 2/-",
]
# Left as stored: a short form inside a longer subject (the parser may compound it with
# another action), one of preference shares, and one without its face values.
LEFT_AS_STORED = [
    "Interim Div Rs 2/Fv Splt Frm Rs 10 To Rs 5",
    "Fv Splt Frm Rs 10 To Rs 2 Ncrps",
    "Fv Splt Frm",
    "Buy Back",
]


def test_short_form_splits_stored_before_the_parser_read_them_are_rederived_as_it_reads_them(
    empty_database_url: str,
) -> None:
    subjects = SHORT_FORMS + LEFT_AS_STORED
    with connect(empty_database_url) as conn:
        apply_migrations(conn)
        # As the #13 database held them before 0019: every one stored as "other".
        conn.execute("DELETE FROM schema_migrations WHERE version = '0019_fv_splt_subjects'")
        for n, subject in enumerate(subjects):
            conn.execute(
                "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind)"
                " VALUES (%s, 'EQ', '2017-01-04', %s, 'other')",
                (f"S{n}", subject),
            )
        conn.commit()

        assert apply_migrations(conn) == ["0019_fv_splt_subjects"]
        rows = conn.execute(
            "SELECT subject, kind, factor_numerator, factor_denominator FROM corporate_actions"
        ).fetchall()

    derived = {
        subject: (kind, None if num is None else Fraction(num, den))
        for subject, kind, num, den in rows
    }
    assert {s: derived[s] for s in SHORT_FORMS} == {s: interpret(s) for s in SHORT_FORMS}
    assert {s: derived[s] for s in LEFT_AS_STORED} == {s: ("other", None) for s in LEFT_AS_STORED}
