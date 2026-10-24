"""The Holdout lock, the family lineage and the Fresh Period (ADR 0007 amendment), over
``synthetic_eod.walk_forward_market`` and ``walk_forward_terms`` (see
``tests/test_walk_forward.py``): three Windows, the last choosing k = 2, r = 2, n = 5, and a
Holdout of 2023-01 (2023-01-02 to 2023-01-31, 21 trading days).

``tradeline backtest holdout --protocol P`` runs the Holdout once per version; assertions go
through ``tradeline.queries``.
"""

import contextlib
import io
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from synthetic_eod import (
    REPO_ROOT,
    WALK_FORWARD_RANGE,
    config_text,
    freeze_protocol,
    load_market,
    repo_calendar,
    walk_forward_market,
    walk_forward_terms,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.holdout import HoldoutRefused, run_holdout, standing
from tradeline.risk import Limits
from tradeline.strategy import Strategy
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes
from tradeline.walk_forward import walk_forward

D = Decimal
CALENDAR = repo_calendar()
TICK_SIZES = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
HOLDOUT = (date(2023, 1, 2), date(2023, 1, 31))
LIMITS = Limits(max_sector=D(1))


def pullback(parameters: Mapping[str, Decimal]) -> Strategy:
    return PullbackV1(k=parameters["k"], r=parameters["r"], n=int(parameters["n"]))


def walked(conn: psycopg.Connection, protocol: str, factory: Any = pullback) -> None:
    walk_forward(conn, factory, protocol=protocol, calendar=CALENDAR, tick_sizes=TICK_SIZES)


def holdout(conn: psycopg.Connection, protocol: str, factory: Any = pullback) -> Any:
    return run_holdout(conn, factory, protocol=protocol, calendar=CALENDAR, tick_sizes=TICK_SIZES)


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_fixtures(walk_forward_market(), tmp_path_factory.mktemp("holdout"), CALENDAR)


@pytest.fixture(scope="module")
def walked_first(database_url: str, fixtures: Path) -> tuple[str, str]:
    """The market loaded, the first ``pullback`` version frozen and walked forward, its
    Holdout not yet run: the database (tests run on copies) and the Protocol's hash."""
    market = walk_forward_market()
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        found = freeze_protocol(
            conn, market, *WALK_FORWARD_RANGE, terms=walk_forward_terms(), limits=LIMITS
        )
        walked(conn, found.hash)
    return database_url, found.hash


@pytest.fixture
def first(walked_first: tuple[str, str], clone_database: Callable[[str], str]) -> tuple[str, str]:
    source, protocol = walked_first
    return clone_database(source), protocol


def holdout_command(url: str, protocol: str, directory: Path) -> int:
    config = directory / "tradeline.toml"
    config.write_text(config_text(walk_forward_market()))
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        return main(["backtest", "holdout", "--protocol", protocol])


def test_the_holdout_runs_once_from_a_fresh_account_under_the_last_windows_parameters(
    first: tuple[str, str], tmp_path: Path
) -> None:
    url, protocol = first

    assert holdout_command(url, protocol, tmp_path) == 0

    with app_connection(url) as conn:
        (attempt,) = queries.holdout_attempts(conn, protocol=protocol)
        assert (attempt.outcome, attempt.family, attempt.consumes, attempt.independent) == (
            "ran",
            "pullback",
            True,
            True,
        )
        assert attempt.parameters == {"k": "2", "r": "2", "n": "5"}
        assert attempt.backtest_id is not None
        stitched = queries.walk_forward(conn, protocol)
        assert stitched is not None and stitched.backtest_id != attempt.backtest_id
        runs = queries.runs(conn, mode="backtest", backtest_id=attempt.backtest_id)
        assert [r.as_of for r in runs] == list(CALENDAR.trading_days(*HOLDOUT))
        assert {r.parameters["n"] for r in runs} == {"5"}
        assert runs[0].cash == D(500000)


def test_a_second_holdout_run_by_the_same_version_is_refused_and_recorded(
    first: tuple[str, str], tmp_path: Path
) -> None:
    url, protocol = first
    with app_connection(url) as conn:
        ran = holdout(conn, protocol)

        with pytest.raises(HoldoutRefused, match="already ran the Holdout"):
            holdout(conn, protocol)

        attempts = queries.holdout_attempts(conn, protocol=protocol)
        assert [a.outcome for a in attempts] == ["ran", "refused"]
        assert attempts[1].backtest_id is None
        assert attempts[1].reason is not None and ran.backtest_id.hex[:8] in attempts[1].reason
        assert {r.backtest_id for r in queries.runs(conn, mode="backtest")} == {
            ran.backtest_id,
            queries.walk_forward(conn, protocol).backtest_id,  # type: ignore[union-attr]
        }
    # The command fails the same way.
    assert holdout_command(url, protocol, tmp_path) == 1


SINGLE_CELL = {"k": (D(2),), "r": (D(2),), "n": (D(5),)}


def later_version(
    conn: psycopg.Connection, *, strategy: Strategy | None = None, factory: Any = pullback
) -> str:
    """Another version frozen now beside the first (a one-cell grid, so another Protocol) and
    walked forward: its Protocol's hash."""
    found = freeze_protocol(
        conn,
        walk_forward_market(),
        *WALK_FORWARD_RANGE,
        terms=replace(walk_forward_terms(), grid=SINGLE_CELL),
        strategy=strategy,
        limits=LIMITS,
    )
    walked(conn, found.hash, factory)
    return found.hash


def test_a_later_version_of_the_family_runs_the_holdout_not_independent_with_its_lineage(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn:
        consumed = holdout(conn, version_1)
        version_2 = later_version(conn)

        ran = holdout(conn, version_2)

        assert (ran.attempt.consumes, ran.attempt.independent) == (False, False)
        assert ran.attempt.corrected_since == ()
        assert queries.runs(conn, mode="backtest", backtest_id=ran.backtest_id)
        lineage = queries.lineage(conn, version_2)
        assert lineage.family.family == "pullback"
        assert [v.protocol for v in lineage.family.versions] == [version_1, version_2]
        assert lineage.family.consumed_by == version_1
        holdouts = [v.holdout for v in lineage.family.versions]
        assert [h.backtest_id for h in holdouts if h is not None] == [
            consumed.backtest_id,
            ran.backtest_id,
        ]
        assert lineage.beside == ()


class Relabelled(PullbackV1):
    """``pullback_v1``'s code under another family's name: a relabelling."""

    __module__ = "tradeline.strategy.pullback"
    family = "pullback_relabelled"


def relabelled(parameters: Mapping[str, Decimal]) -> Strategy:
    return Relabelled(k=parameters["k"], r=parameters["r"], n=int(parameters["n"]))


def test_a_version_relabelled_as_a_new_family_shows_the_family_it_was_frozen_beside(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn:
        holdout(conn, version_1)
        version_2 = later_version(conn, strategy=Relabelled(), factory=relabelled)

        ran = holdout(conn, version_2, relabelled)

        # Visible, not policed: its new family's Holdout is its own to consume.
        assert (ran.attempt.family, ran.attempt.consumes, ran.attempt.independent) == (
            "pullback_relabelled",
            True,
            True,
        )
        lineage = queries.lineage(conn, version_2)
        assert lineage.family.family == "pullback_relabelled"
        assert [v.protocol for v in lineage.family.versions] == [version_2]
        (beside,) = lineage.beside
        assert (beside.family, beside.consumed_by) == ("pullback", version_1)
        assert [v.protocol for v in beside.versions] == [version_1]
        # The first version's lineage shows nothing frozen beside it before it.
        assert queries.lineage(conn, version_1).beside == ()


def test_a_not_independent_run_cannot_be_marked_passable_without_twelve_fresh_months(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn:
        holdout(conn, version_1)
        version_2 = later_version(conn)
        holdout(conn, version_2)
        frozen_on = queries.lineage(conn, version_2).frozen_at.astimezone(IST).date()

        consumer = standing(conn, version_1, calendar=CALENDAR)
        later = standing(conn, version_2, calendar=CALENDAR)

    assert (consumer.may_pass, consumer.fresh_period) == (True, None)
    assert later.may_pass is False
    period = later.fresh_period
    assert period is not None
    # After both 2023-01-31 and the freeze (today), so none of the market's data, which
    # ends 2023-01-31, is in it (and none at all once the Trading Calendar runs out).
    assert period.first is None or period.first > frozen_on
    assert (period.last, period.trading_days, period.long_enough) == (None, 0, False)
    assert "twelve months" in later.reason


def test_the_application_role_cannot_change_or_remove_a_holdout_attempt(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn:
        holdout(conn, version_1)
        for statement in (
            "UPDATE holdout_attempts SET consumes = false",
            "DELETE FROM holdout_attempts",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_standing_is_refused_for_a_version_that_has_not_run_the_holdout(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn, pytest.raises(ValueError, match="not run the Holdout"):
        standing(conn, version_1, calendar=CALENDAR)


def correct_ash_in_the_holdout(url: str) -> None:
    """A data correction on the seen Holdout: ASH's close on 2023-01-16 moves. It lies in no
    Universe window the Protocols cover (2023-01's is December's), so only ASH's Adjusted
    Series hash changes, and the earlier Protocol's snapshot no longer verifies."""
    with psycopg.connect(url) as owner:
        owner.execute(
            "UPDATE eod_bhavcopy SET close = close + 1"
            " WHERE symbol = 'ASH' AND trade_date = '2023-01-16'"
        )


def test_a_rerun_after_a_data_correction_is_not_independent_under_the_same_terms(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn:
        consumed = holdout(conn, version_1)
    correct_ash_in_the_holdout(url)
    with app_connection(url) as conn:
        # The same Strategy and terms frozen again over the corrected data.
        again = freeze_protocol(
            conn,
            walk_forward_market(),
            *WALK_FORWARD_RANGE,
            terms=walk_forward_terms(),
            limits=LIMITS,
        ).hash
        assert again != version_1
        walked(conn, again)

        ran = holdout(conn, again)

        assert ran.attempt.corrected_since == (consumed.attempt.id,)
        assert (ran.attempt.consumes, ran.attempt.independent) == (False, False)


def test_a_new_familys_first_holdout_run_after_a_data_correction_is_not_independent(
    first: tuple[str, str],
) -> None:
    url, version_1 = first
    with app_connection(url) as conn:
        consumed = holdout(conn, version_1)
    correct_ash_in_the_holdout(url)
    with app_connection(url) as conn:
        version_2 = later_version(conn, strategy=Relabelled(), factory=relabelled)

        ran = holdout(conn, version_2, relabelled)

        # It consumes its family's Holdout, but on a period seen before the correction.
        assert ran.attempt.corrected_since == (consumed.attempt.id,)
        assert (ran.attempt.consumes, ran.attempt.independent) == (True, False)
        (attempt,) = queries.holdout_attempts(conn, family="pullback_relabelled")
        assert attempt.independent is False
