"""Versioned schema migrations.

Each migration is one SQL file in ``sql/`` named ``NNNN_description.sql``,
applied in order inside its own transaction together with its row in
``schema_migrations``. Applying is idempotent: already-recorded versions are
skipped. Migrations are never edited once committed; a change is a new file.
"""

from __future__ import annotations

import logging
from importlib import resources

import psycopg

log = logging.getLogger(__name__)

# Serialises concurrent ``migrate`` runs against one database.
_LOCK_KEY = 0x7472_6164  # "trad"

# The migration that lets ``tradeline_app`` read which migrations are applied.
SCHEMA_MIGRATIONS_READ = "0022_schema_migrations_read"


def available_migrations() -> list[tuple[str, str]]:
    """``(version, sql)`` pairs in application order."""
    sql_dir = resources.files(__package__) / "sql"
    files = sorted((p for p in sql_dir.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name.removesuffix(".sql"), p.read_text()) for p in files]


def applied_migrations(conn: psycopg.Connection) -> list[str]:
    try:
        with conn.transaction():
            rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    except psycopg.errors.UndefinedTable:
        return []
    return [str(version) for (version,) in rows]


_RUN_MIGRATE = "run `tradeline migrate`"


class PendingMigrations(RuntimeError):
    """The database is behind the code: a command would read a schema it lacks."""

    def __init__(self, versions: list[str]) -> None:
        count = len(versions)
        super().__init__(
            f"{count} migration{'s' if count != 1 else ''} pending ({', '.join(versions)});"
            f" {_RUN_MIGRATE}"
        )


class UnreadableMigrations(RuntimeError):
    """The connecting role cannot tell whether a migration is pending (#44)."""

    def __init__(self) -> None:
        super().__init__(
            f"cannot read schema_migrations, which {SCHEMA_MIGRATIONS_READ} grants; {_RUN_MIGRATE}"
        )


def pending_migrations(conn: psycopg.Connection) -> list[str]:
    """The versions this code has that the database has not applied, in order."""
    done = set(applied_migrations(conn))
    return [version for version, _ in available_migrations() if version not in done]


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply every pending migration; return the versions applied in this call."""
    applied: list[str] = []
    with conn.transaction():
        # The lock comes first so that two first-ever runs cannot both create
        # the tracking table or both apply a migration.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _ensure_tracking_table(conn)
        done = set(applied_migrations(conn))
        for version, sql in available_migrations():
            if version in done:
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            log.info("applied migration", extra={"migration": version})
            applied.append(version)
    return applied


def _ensure_tracking_table(conn: psycopg.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version text PRIMARY KEY,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
