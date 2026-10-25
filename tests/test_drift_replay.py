"""The Drift Replay (ADR 0007, 2026-10-25 amendment): ``tradeline backtest drift --protocol
P`` at a commit other than the frozen one reruns the Stitched Curve and the Fresh Period
days and compares them with the runs at the frozen commit, read back through
``tradeline.queries`` and the full report.

The market and versions are ``tests/test_gate.py``'s: a first ``pullback`` version consumes
the Holdout (2023-01), and a later one, frozen on the Holdout's last day, is Not
Independent and has run its Fresh Period from 2023-02-01 to 2024-02-29. The replays below
are of the later version, which has all three curves. The suite stands in at
``checkout.TEST_COMMIT``; a replay stands in at ``LATER`` instead, as a later checkout of
the engine would be.
"""

import contextlib
import io
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from checkout import TEST_COMMIT
from synthetic_eod import (
    compute_universe_months,
    config_text,
    load_market,
    repo_calendar,
    write_fixtures,
)
from test_gate import (
    FRESH_END,
    FRESH_FIRST,
    PASSING,
    SAME_CHOICES,
    fresh_command,
    frozen_on_the_holdouts_last_day,
    gate_market,
    report_command,
    run_version,
)
from tradeline import queries, revision
from tradeline.cli import main
from tradeline.costs import CostModel
from tradeline.db import app_connection
from tradeline.protocol import Protocol

D = Decimal
CALENDAR = repo_calendar()
LATER = "1a7e" * 10


@pytest.fixture(scope="module")
def versions(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """The later version with its Stitched Curve, Holdout and Fresh Period run, and its
    report: the database (tests run on copies) and the version's hash."""
    market = gate_market()
    fixtures = write_fixtures(market, tmp_path_factory.mktemp("drift"), CALENDAR)
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
    first = run_version(database_url, PASSING)
    later = run_version(database_url, PASSING, grid=SAME_CHOICES)
    frozen_on_the_holdouts_last_day(database_url, first, later)
    with app_connection(database_url) as conn:
        compute_universe_months(conn, market, FRESH_FIRST, FRESH_END)
    directory = tmp_path_factory.mktemp("drift_commands")
    code, _ = fresh_command(database_url, later, directory)
    assert code == 0
    report_command(database_url, later, directory)
    return database_url, later


@pytest.fixture
def later(versions: tuple[str, str], clone_database: Callable[[str], str]) -> tuple[str, str]:
    source, protocol = versions
    return clone_database(source), protocol


@pytest.fixture
def at_a_later_commit() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(revision, "running_commit", lambda: LATER)
        yield


def drift_command(url: str, protocol: str, directory: Path, *curves: str) -> tuple[int, str]:
    """``backtest drift --protocol``: its exit code and what it logged."""
    config = directory / "tradeline.toml"
    config.write_text(config_text(gate_market()))
    out = io.StringIO()
    arguments = ["backtest", "drift", "--protocol", protocol]
    for curve in curves:
        arguments += ["--curve", curve]
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(out):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        code = main(arguments)
    return code, out.getvalue()


def stt_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for a later commit whose engine prices STT at twice the Protocol's rate."""
    frozen_model = Protocol.cost_model

    def cost_model(self: Protocol) -> CostModel:
        model = frozen_model(self)
        rates = [
            replace(r, rate=r.rate * 2) if r.charge == "stt" else r
            for r in model.schedule.rates_by_date
        ]
        return replace(model, schedule=replace(model.schedule, rates_by_date=tuple(rates)))

    monkeypatch.setattr(Protocol, "cost_model", cost_model)


def accounts(url: str, protocol: str) -> dict[str, Any]:
    with app_connection(url) as conn:
        walked = queries.walk_forward(conn, protocol)
        fresh = queries.fresh_period_run(conn, protocol)
        assert walked is not None and fresh is not None
        return {"stitched_curve": walked.backtest_id, "fresh_period": fresh.backtest_id}


def days(url: str, backtest_id: Any) -> list[Any]:
    with app_connection(url) as conn:
        return [r.as_of for r in queries.runs(conn, mode="backtest", backtest_id=backtest_id)]


def test_at_a_later_commit_with_no_behavioural_change_the_replay_is_equal_and_a_pass(
    later: tuple[str, str], at_a_later_commit: None, tmp_path: Path
) -> None:
    url, protocol = later

    code, _ = drift_command(url, protocol, tmp_path)

    assert code == 0
    with app_connection(url) as conn:
        (replay,) = queries.drift_replays(conn, protocol=protocol)
    assert (replay.git_commit, replay.frozen_commit) == (LATER, TEST_COMMIT)
    assert (replay.outcome, replay.differences) == ("pass", [])
    assert replay.curves == ("stitched_curve", "fresh_period")
    frozen = accounts(url, protocol)
    # Each curve rerun whole in an account of its own, over the frozen run's days.
    for curve, account in replay.accounts.items():
        assert account != frozen[curve]
        assert days(url, account) == days(url, frozen[curve])
    assert days(url, replay.accounts["fresh_period"])[0] == FRESH_FIRST
    with app_connection(url) as conn:
        assert queries.drift_replay_passed(conn, protocol=protocol, git_commit=LATER)
        assert not queries.drift_replay_passed(conn, protocol=protocol, git_commit="0" * 40)


def test_with_a_changed_cost_rate_the_money_difference_is_reported_per_fill(
    later: tuple[str, str], at_a_later_commit: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, protocol = later
    stt_doubled(monkeypatch)

    code, logged = drift_command(url, protocol, tmp_path)

    assert code == 1
    assert "drift replay failed" in logged
    with app_connection(url) as conn:
        (replay,) = queries.drift_replays(conn, protocol=protocol)
        frozen_fills = {
            curve: queries.fills(conn, mode="backtest", backtest_id=account)
            for curve, account in accounts(url, protocol).items()
        }
        replayed = {
            curve: {
                f"{f.day} {f.symbol} {f.side}": f
                for f in queries.fills(conn, mode="backtest", backtest_id=account)
            }
            for curve, account in replay.accounts.items()
        }
        assert not queries.drift_replay_passed(conn, protocol=protocol, git_commit=LATER)
    assert replay.outcome == "fail" and replay.differences is not None
    stt = [d for d in replay.differences if d["kind"] == "costs" and d["field"] == "stt"]
    # Every Fill pays more STT, so each is one difference: the frozen and the replayed rupees.
    for curve, fills in frozen_fills.items():
        found = {d["key"]: d for d in stt if d["curve"] == curve}
        assert len(found) == len(fills) > 0
        for fill in fills:
            key = f"{fill.day} {fill.symbol} {fill.side}"
            again = replayed[curve][key]
            assert fill.charges is not None and again.charges is not None
            # Twice the rate on the replayed Fill's own value, to the rupee.
            doubled = again.price * again.shares * D("0.002")
            assert again.charges.stt == doubled.quantize(D(1), ROUND_HALF_UP)
            assert (D(found[key]["frozen"]), D(found[key]["replay"])) == (
                fill.charges.stt,
                again.charges.stt,
            )
    assert all(d["money"] for d in replay.differences)


def test_a_drift_replay_naming_the_holdout_is_refused_and_recorded_as_an_attempt(
    later: tuple[str, str], at_a_later_commit: None, tmp_path: Path
) -> None:
    url, protocol = later
    with app_connection(url) as conn:
        before = len(queries.runs(conn, mode="backtest"))

    code, logged = drift_command(url, protocol, tmp_path, "stitched_curve", "holdout")

    assert code == 1
    assert "never runs the Holdout" in logged
    with app_connection(url) as conn:
        (replay,) = queries.drift_replays(conn, protocol=protocol)
        assert len(queries.runs(conn, mode="backtest")) == before
        assert not queries.drift_replay_passed(conn, protocol=protocol, git_commit=LATER)
    assert replay.outcome == "refused"
    assert replay.curves == ("stitched_curve", "holdout")
    assert replay.accounts == {}
    assert replay.reason is not None and "Holdout" in replay.reason


def test_a_second_request_at_the_same_commit_reads_the_cached_result(
    later: tuple[str, str], at_a_later_commit: None, tmp_path: Path
) -> None:
    url, protocol = later
    assert drift_command(url, protocol, tmp_path)[0] == 0
    with app_connection(url) as conn:
        before = len(queries.runs(conn, mode="backtest"))

    code, logged = drift_command(url, protocol, tmp_path)

    assert code == 0
    assert '"cached": true' in logged
    with app_connection(url) as conn:
        (replay,) = queries.drift_replays(conn, protocol=protocol)
        assert len(queries.runs(conn, mode="backtest")) == before
    assert replay.outcome == "pass"


def test_a_drift_replay_at_the_frozen_commit_is_refused(
    later: tuple[str, str], tmp_path: Path
) -> None:
    url, protocol = later

    code, logged = drift_command(url, protocol, tmp_path)

    assert code == 1
    assert "frozen at this commit" in logged
    with app_connection(url) as conn:
        assert queries.drift_replays(conn, protocol=protocol) == []


def test_the_gate_never_reads_a_drift_replay(later: tuple[str, str], tmp_path: Path) -> None:
    url, protocol = later
    before, _ = report_command(url, protocol, tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(revision, "running_commit", lambda: LATER)
        stt_doubled(patch)
        assert drift_command(url, protocol, tmp_path)[0] == 1

    after, _ = report_command(url, protocol, tmp_path)

    with app_connection(url) as conn:
        (replay,) = queries.drift_replays(conn, protocol=protocol)
        evaluations = queries.gate_evaluations(conn, protocol=protocol)
        windows = queries.walk_forward_windows(conn, protocol)
    assert replay.outcome == "fail"
    assert after["gate"] == before["gate"]
    for curve in ("stitched_curve", "holdout"):
        assert after[curve] == before[curve]
    replayed = set(replay.accounts.values())
    assert not replayed & {e.stitched_backtest_id for e in evaluations}
    assert len(windows) == 3


def test_the_application_role_cannot_change_or_remove_a_drift_replay(
    later: tuple[str, str], at_a_later_commit: None, tmp_path: Path
) -> None:
    url, protocol = later
    drift_command(url, protocol, tmp_path, "holdout")

    with app_connection(url) as conn:
        for statement in (
            "UPDATE drift_replays SET outcome = 'pass'",
            "DELETE FROM drift_replays",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
