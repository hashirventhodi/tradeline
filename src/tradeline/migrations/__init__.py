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


def available_migrations() -> list[tuple[str, str]]:
    """``(version, sql)`` pairs in application order."""
    sql_dir = resources.files(__package__) / "sql"
    files = sorted((p for p in sql_dir.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name.removesuffix(".sql"), p.read_text()) for p in files]


def applied_migrations(conn: psycopg.Connection) -> list[str]:
    _ensure_tracking_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [str(version) for (version,) in rows]


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply every pending migration; return the versions applied in this call."""
    _ensure_tracking_table(conn)
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
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
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
