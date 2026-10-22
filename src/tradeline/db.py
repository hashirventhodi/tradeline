"""Postgres connections.

Two kinds. ``connect`` is the owner connection used by ``migrate``; it owns the
schema. ``app_connection`` is what every command and query uses: it switches to
the ``tradeline_app`` role, which has no UPDATE or DELETE on the tables that are
append-only, so append-only is enforced by the database and not by convention.

An app connection to a database with a migration pending is refused, naming
the pending versions: a command run before ``migrate`` would otherwise fail on
a view or column it lacks, with an error that does not say why. The check reads
``schema_migrations`` before switching role, which the schema's owner and any
member of ``tradeline_app`` can (migration 0022); a member without INHERIT reads
it after switching. A member-only role meeting a database without 0022 cannot
read it at all, and is told to migrate. A login role that is not a member of
``tradeline_app`` is refused the switch, before or after the check, and is told
to be granted the role: migrating would not help it.

Locally one ``DATABASE_URL`` serves both because the Compose user owns the
schema and is a member of ``tradeline_app``. In production (Phase 6) the app
process can be given a login role that is only a member of ``tradeline_app``.
"""

from __future__ import annotations

import psycopg

from tradeline.migrations import PendingMigrations, UnreadableMigrations, pending_migrations

APP_ROLE = "tradeline_app"


class NotAppRoleMember(RuntimeError):
    """The connecting login role may not switch to ``tradeline_app`` (#47)."""

    def __init__(self, user: str) -> None:
        # ``user`` is quoted as an identifier, so the GRANT can be pasted as printed.
        super().__init__(
            f"login role {user} is not a member of {APP_ROLE};"
            f" grant it with `GRANT {APP_ROLE} TO {user}`"
        )


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def app_connection(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url)
    try:
        pending = _pending_migrations_as_member(conn)
        if pending:
            raise PendingMigrations(pending)
        _set_app_role(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _pending_migrations_as_member(conn: psycopg.Connection) -> list[str]:
    """Read before SET ROLE, so the owner sees every pending version even before 0022.

    A role that cannot read ``schema_migrations`` reads it again as ``tradeline_app``:
    a member without INHERIT holds that role's grants only after SET ROLE. If that
    is refused too, 0022 is most likely pending.
    """
    try:
        return pending_migrations(conn)
    except psycopg.errors.InsufficientPrivilege:
        pass
    _set_app_role(conn)
    try:
        return pending_migrations(conn)
    except psycopg.errors.InsufficientPrivilege:
        raise UnreadableMigrations() from None


def _set_app_role(conn: psycopg.Connection) -> None:
    try:
        conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(APP_ROLE)))
    except psycopg.errors.InsufficientPrivilege:
        raise NotAppRoleMember(psycopg.sql.Identifier(conn.info.user).as_string(conn)) from None
    conn.commit()
