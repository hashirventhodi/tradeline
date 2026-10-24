"""Test harness.

Every test runs under the test environment: the live transport refuses to be
built and the secrets provider never reads the repo's ``.env``.

Database tests get a fresh database per module. Migrations run once per session
into a template database; each module clones it, which takes milliseconds. The
Postgres comes from ``docker compose up postgres`` (or CI's service container)
and is reached through ``TRADELINE_TEST_DATABASE_URL``, a maintenance URL whose
database is never written to.
"""

import logging
import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

os.environ["TRADELINE_ENV"] = "test"

from checkout import TEST_COMMIT  # noqa: E402
from tradeline import revision  # noqa: E402
from tradeline.db import connect  # noqa: E402
from tradeline.migrations import apply_migrations  # noqa: E402

MAINTENANCE_URL = os.environ.get(
    "TRADELINE_TEST_DATABASE_URL", "postgresql://tradeline:tradeline@127.0.0.1:55432/postgres"
)


def _create_database(name: str, *, template: str | None = None) -> str:
    statement = psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name))
    if template is not None:
        statement += psycopg.sql.SQL(" TEMPLATE {}").format(psycopg.sql.Identifier(template))
    with psycopg.connect(MAINTENANCE_URL, autocommit=True) as conn:
        conn.execute(statement)
    return psycopg.conninfo.make_conninfo(MAINTENANCE_URL, dbname=name)


def _drop_database(name: str) -> None:
    with psycopg.connect(MAINTENANCE_URL, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                psycopg.sql.Identifier(name)
            )
        )


def _database_name(label: str) -> str:
    """A unique Postgres name: the label is cut to fit 63 bytes, never the random suffix."""
    prefix, suffix = "tradeline_test_", f"_{secrets.token_hex(4)}"
    return f"{prefix}{label[: 63 - len(prefix) - len(suffix)]}{suffix}".lower()


@pytest.fixture(autouse=True, scope="session")
def _at_test_commit() -> Iterator[None]:
    """Every test runs the code as if checked out clean at ``TEST_COMMIT`` (the working
    tree is often dirty while code is written), module fixtures included; the tests of a
    dirty tree's refusal point it at a checkout of their own (``checkout.py``)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(revision, "running_commit", lambda: TEST_COMMIT)
        yield


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Drop handlers a test configured so no later test logs into a closed stream."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


@pytest.fixture(scope="session")
def migrated_template() -> Iterator[str]:
    name = _database_name("template")
    url = _create_database(name)
    with connect(url) as conn:
        apply_migrations(conn)
    yield name
    _drop_database(name)


@pytest.fixture(scope="module")
def database_url(request: pytest.FixtureRequest, migrated_template: str) -> Iterator[str]:
    """A migrated, otherwise empty database for this module."""
    name = _database_name(Path(request.module.__file__).stem)
    yield _create_database(name, template=migrated_template)
    _drop_database(name)


@pytest.fixture
def fresh_database_url(request: pytest.FixtureRequest, migrated_template: str) -> Iterator[str]:
    """A migrated, otherwise empty database for this test alone, when tests load different data."""
    name = _database_name(f"fresh_{request.node.name}")
    yield _create_database(name, template=migrated_template)
    _drop_database(name)


@pytest.fixture
def empty_database_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """A database with no schema at all, for tests of ``migrate`` itself. Per test."""
    name = _database_name(f"empty_{request.node.name}")
    yield _create_database(name)
    _drop_database(name)


@pytest.fixture
def another_database_url(request: pytest.FixtureRequest, migrated_template: str) -> Iterator[str]:
    """A second migrated, empty database, per test, for comparing independent runs."""
    name = _database_name(f"another_{request.node.name}")
    yield _create_database(name, template=migrated_template)
    _drop_database(name)


@pytest.fixture(scope="session")
def clone_database() -> Iterator[Callable[[str], str]]:
    """Copy a database as it stands (nothing may be connected to it), for runs that must
    start from one loaded state, the same ingest runs and Protocol included. Each copy is
    dropped at the end of the session."""
    names: list[str] = []

    def clone(url: str) -> str:
        source = psycopg.conninfo.conninfo_to_dict(url)["dbname"]
        assert isinstance(source, str)
        names.append(_database_name(f"copy_{source.removeprefix('tradeline_test_')}"))
        return _create_database(names[-1], template=source)

    yield clone
    for name in names:
        _drop_database(name)


@pytest.fixture
def member_only_url(request: pytest.FixtureRequest) -> Iterator[Callable[..., str]]:
    """Make a login role that is only a member of tradeline_app and return ``url`` as that role.

    The production shape (db.py). ``member=False`` leaves out the grant, as a login role
    someone forgot to set up (#47). Roles are cluster-wide, so each is dropped after the test.
    """
    names: list[str] = []

    def make(url: str, *, inherit: bool = True, member: bool = True) -> str:
        name = _database_name(f"role_{request.node.name}")
        password = secrets.token_hex(8)
        with psycopg.connect(MAINTENANCE_URL, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}").format(
                    psycopg.sql.Identifier(name),
                    psycopg.sql.SQL("INHERIT" if inherit else "NOINHERIT"),
                    psycopg.sql.Literal(password),
                )
            )
            names.append(name)
            if member:
                conn.execute(
                    psycopg.sql.SQL("GRANT tradeline_app TO {}").format(
                        psycopg.sql.Identifier(name)
                    )
                )
        return psycopg.conninfo.make_conninfo(url, user=name, password=password)

    yield make
    with psycopg.connect(MAINTENANCE_URL, autocommit=True) as conn:
        for name in names:
            conn.execute(
                psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(name))
            )
