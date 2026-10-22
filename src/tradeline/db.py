"""Postgres connections.

Two kinds. ``connect`` is the owner connection used by ``migrate``; it owns the
schema. ``app_connection`` is what every command and query uses: it switches to
the ``tradeline_app`` role, which has no UPDATE or DELETE on the tables that are
append-only, so append-only is enforced by the database and not by convention.

An app connection to a database with a migration pending is refused, naming
them: a command run before ``migrate`` would otherwise fail on a view or column
it lacks, with an error that does not say why. The check reads
``schema_migrations`` before switching role, as the login role.

Locally one ``DATABASE_URL`` serves both because the Compose user owns the
schema and is a member of ``tradeline_app``. In production (Phase 6) the app
process can be given a login role that is only a member of ``tradeline_app``.
"""

from __future__ import annotations

import psycopg

from tradeline.migrations import PendingMigrations, pending_migrations

APP_ROLE = "tradeline_app"


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def app_connection(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url)
    try:
        pending = pending_migrations(conn)
        if pending:
            raise PendingMigrations(pending)
        conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(APP_ROLE)))
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn
