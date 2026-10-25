"""Machinery Corrections (ADR 0007 amendment of 2026-10-25), through the application
services: ``protocol freeze`` as a correction, the Holdout and Fresh Period under the new
Protocol, and ``backtest report``, read back through ``tradeline.queries`` and the report.

The version corrected is ``test_gate``'s later, Not Independent version with its Stitched
Curve, Holdout, Fresh Period and report, all at ``checkout.TEST_COMMIT``. The fix stands in
at ``LATER``: an engine that prices STT at twice the Protocol's rate (``stt_doubled``),
as if the frozen commit had read the rate wrong and a contract note had shown it.
"""

import contextlib
import io
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from checkout import committed_checkout, git
from synthetic_eod import (
    WALK_FORWARD_RANGE,
    compute_universe_months,
    config_text,
    freeze_protocol,
    load_market,
    repo_calendar,
    walk_forward_terms,
    write_fixtures,
)
from test_drift_replay import LATER, stt_doubled
from test_gate import (
    FAILING,
    FRESH_END,
    FRESH_FIRST,
    HALTS,
    LIMITS,
    PASSING,
    SAME_CHOICES,
    TICK_SIZES,
    fresh_command,
    frozen_on_the_holdouts_last_day,
    gate_market,
    pullback,
    report_command,
    run_version,
)
from tradeline import freeze, queries, revision
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.protocol import Correction, Evidence
from tradeline.protocol_report import read_protocol_report
from tradeline.walk_forward import walk_forward

D = Decimal
CALENDAR = repo_calendar()
EVIDENCE = Evidence(
    test="tests/test_costs.py::test_stt_on_a_delivery_sale_matches_the_contract_note",
    record="contract_note",
    citation="Zerodha contract note CNT-24-0001, 2024-01-15",
    failed_at="f00d" * 10,
)
DESCRIPTION = "STT is charged on both sides of a delivery trade, as the contract note shows"
# What pytest said of the evidence's test at its commit and at the fix, as ``fixed`` stands
# them in.
FAILED = revision.TestOutcome(False, "1 failed in 0.01s")
PASSED = revision.TestOutcome(True, "1 passed in 0.01s")


@pytest.fixture(scope="module")
def versions(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """``test_gate``'s later version with its Stitched Curve, Holdout, Fresh Period and
    report: the database and the version's hash."""
    market = gate_market()
    fixtures = write_fixtures(market, tmp_path_factory.mktemp("correction"), CALENDAR)
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
    first = run_version(database_url, PASSING)
    later = run_version(database_url, PASSING, grid=SAME_CHOICES)
    frozen_on_the_holdouts_last_day(database_url, first, later)
    with app_connection(database_url) as conn:
        compute_universe_months(conn, market, FRESH_FIRST, FRESH_END)
    directory = tmp_path_factory.mktemp("correction_commands")
    code, _ = fresh_command(database_url, later, directory)
    assert code == 0
    report_command(database_url, later, directory)
    return database_url, later


@contextlib.contextmanager
def fixed() -> Iterator[None]:
    """At ``LATER``, the fixed commit, whose engine doubles STT; its evidence taken as
    written first, failing there and passing at the fix
    (``test_the_evidence_must_be_written_and_failing_before_the_fix`` runs that against a
    real checkout)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(revision, "running_commit", lambda: LATER)
        patch.setattr(revision, "test_failed_before", lambda *args, **kwargs: (FAILED, PASSED))
        stt_doubled(patch)
        yield


def correct(
    conn: psycopg.Connection, corrected: str, *, evidence: Evidence = EVIDENCE, **changes: Any
) -> freeze.FreezeResult:
    """Freeze the Machinery Correction of ``corrected`` at ``LATER``."""
    return freeze_protocol(
        conn,
        gate_market(),
        *WALK_FORWARD_RANGE,
        terms=changes.pop("terms", replace(walk_forward_terms(), gate=PASSING, grid=SAME_CHOICES)),
        limits=LIMITS,
        halts=HALTS,
        commit=changes.pop("commit", LATER),
        correction=Correction(corrected, DESCRIPTION, evidence),
    )


@pytest.fixture
def later(versions: tuple[str, str], clone_database: Callable[[str], str]) -> tuple[str, str]:
    source, protocol = versions
    return clone_database(source), protocol


def test_a_correction_with_a_changed_strategy_hash_is_refused_and_nothing_frozen(
    later: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, protocol = later
    monkeypatch.setattr(freeze, "code_revision", lambda module: "c0de" * 16)

    with fixed(), app_connection(url) as conn, pytest.raises(ValueError) as refused:
        correct(conn, protocol)

    assert "Strategy" in str(refused.value)
    assert "not a Machinery Correction" in str(refused.value)
    with app_connection(url) as conn:
        assert len(queries.protocols(conn)) == 2


def test_a_correction_is_recorded_with_its_evidence_and_the_protocol_it_corrects(
    later: tuple[str, str],
) -> None:
    url, protocol = later

    with fixed(), app_connection(url) as conn:
        result = correct(conn, protocol[:12])

    with app_connection(url) as conn:
        frozen = queries.protocol(conn, result.hash)
    assert frozen is not None
    assert frozen.protocol.git_commit == LATER
    recorded = replace(EVIDENCE, failed=FAILED.summary, passed=PASSED.summary)
    assert frozen.protocol.correction == Correction(protocol, DESCRIPTION, recorded)
    stated = frozen.protocol.content()["correction"]["evidence"]
    assert (stated["record"], stated["failed"], stated["passed"]) == (
        "contract_note",
        "1 failed in 0.01s",
        "1 passed in 0.01s",
    )


def test_the_evidence_must_be_written_and_failing_before_the_fix(tmp_path: Path) -> None:
    """The test is run by pytest at each commit, in a checkout of its own: it must fail
    where it was written and pass at the fix, and what pytest said at each is returned."""
    checkout = committed_checkout(tmp_path / "repo")
    (checkout / "tests").mkdir()
    (checkout / "tests" / "test_costs.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_stt_matches_the_contract_note():\n"
        '    assert "STT_SIDES = 2" in (Path(__file__).parents[1] / "code.py").read_text()\n\n\n'
        "def test_already_passing():\n    assert True\n"
    )
    git(checkout, "add", "tests/test_costs.py")
    git(checkout, "commit", "--quiet", "-m", "Check STT against the contract note")
    failing = git(checkout, "rev-parse", "HEAD").strip()
    (checkout / "code.py").write_text("STT_SIDES = 2\n")
    git(checkout, "commit", "--quiet", "-am", "Charge STT on both sides")
    fix = git(checkout, "rev-parse", "HEAD").strip()
    (checkout / "code.py").write_text("STT_SIDES = 1\n")
    git(checkout, "commit", "--quiet", "-am", "Charge STT on one side again")
    undone = git(checkout, "rev-parse", "HEAD").strip()
    test = "tests/test_costs.py::test_stt_matches_the_contract_note"

    failed, passed = revision.test_failed_before(test, failing, fix, directory=checkout)

    assert (failed.passed, passed.passed) == (False, True)
    assert "1 failed" in failed.summary and "1 passed" in passed.summary
    refusals = [
        ((test, fix, failing), "is not before"),  # the fix before the test
        ((test, fix, fix), "is not before"),  # written with the fix
        (("tests/test_costs.py::test_another", failing, fix), "defines no test"),
        (("tests/test_tax.py::test_stt_matches_the_contract_note", failing, fix), "no test file"),
        ((test, "0" * 40, fix), "cannot read commit"),
        (("tests/test_costs.py::test_already_passing", failing, fix), "passed at"),
        ((test, failing, undone), "did not pass at"),  # the fix does not fix it
    ]
    for arguments, refused in refusals:
        with pytest.raises(ValueError, match=refused):
            revision.test_failed_before(*arguments, directory=checkout)
    # Each run's checkout is removed after it.
    assert git(checkout, "worktree", "list").count("\n") == 1


def test_a_correction_at_the_corrected_protocols_own_commit_is_refused(
    later: tuple[str, str],
) -> None:
    url, protocol = later

    with fixed(), app_connection(url) as conn, pytest.raises(ValueError, match="fixed commit"):
        correct(conn, protocol, commit=queries_commit(url, protocol))


def queries_commit(url: str, protocol: str) -> str:
    with app_connection(url) as conn:
        frozen = queries.protocol(conn, protocol)
        assert frozen is not None
        return frozen.protocol.git_commit


def test_a_correction_changing_the_holdout_or_the_gate_is_refused(
    later: tuple[str, str],
) -> None:
    url, protocol = later
    terms = replace(walk_forward_terms(), gate=replace(PASSING, sharpe_above=Decimal("0.4")))
    terms = replace(terms, grid=SAME_CHOICES)

    with fixed(), app_connection(url) as conn, pytest.raises(ValueError, match="gate"):
        correct(conn, protocol, terms=terms)


def test_a_correction_of_a_version_that_has_not_run_the_holdout_is_refused(
    later: tuple[str, str],
) -> None:
    url, _ = later
    with app_connection(url) as conn:
        unrun = freeze_protocol(
            conn,
            gate_market(),
            *WALK_FORWARD_RANGE,
            terms=replace(walk_forward_terms(), gate=FAILING),
            limits=LIMITS,
            halts=HALTS,
        ).hash

    with fixed(), app_connection(url) as conn, pytest.raises(ValueError, match="not run"):
        correct(conn, unrun, terms=replace(walk_forward_terms(), gate=FAILING))


@pytest.fixture(scope="module")
def corrected(
    versions: tuple[str, str],
    clone_database: Callable[[str], str],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str, str]:
    """The correction frozen at ``LATER``, its Walk-Forward run and its Fresh Period
    continued there: the database, the corrected Protocol and the correcting one."""
    source, original = versions
    url = clone_database(source)
    with fixed(), app_connection(url) as conn:
        correction = correct(conn, original).hash
        walk_forward(conn, pullback, protocol=correction, calendar=CALENDAR, tick_sizes=TICK_SIZES)
    with fixed():
        code, _ = fresh_command(url, correction, tmp_path_factory.mktemp("corrected"))
    assert code == 0
    return url, original, correction


def test_the_holdout_is_not_run_again_under_the_correction(
    corrected: tuple[str, str, str], clone_database: Callable[[str], str], tmp_path: Path
) -> None:
    url, original, correction = corrected
    url = clone_database(url)
    config = tmp_path / "tradeline.toml"
    config.write_text(config_text(gate_market()))

    with fixed(), pytest.MonkeyPatch.context() as patch:
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["backtest", "holdout", "--protocol", correction])

    assert code == 1
    with app_connection(url) as conn:
        (refused,) = queries.holdout_attempts(conn, protocol=correction)
        assert refused.outcome == "refused" and refused.reason is not None
        assert "stands as recorded" in refused.reason
        assert [a.outcome for a in queries.holdout_attempts(conn, protocol=original)] == ["ran"]


@pytest.fixture(scope="module")
def reports(
    corrected: tuple[str, str, str],
    clone_database: Callable[[str], str],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The corrected version's report, as written before the correction, and the
    correction's report with its Markdown."""
    url, original, correction = corrected
    url = clone_database(url)
    with app_connection(url) as conn:
        before = read_protocol_report(conn, original).as_json()
    with fixed():
        after, markdown = report_command(url, correction, tmp_path_factory.mktemp("reports"))
    return before, after, markdown


def test_the_gate_reads_the_corrected_versions_holdout_run_as_recorded(
    reports: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    before, after, _ = reports

    assert after["holdout"] == before["holdout"]
    holdout = [c for c in after["gate"]["checks"] if c["curve"] == "holdout"]
    assert holdout == [c for c in before["gate"]["checks"] if c["curve"] == "holdout"]
    assert after["gate"]["standing"] == before["gate"]["standing"]
    found = after["machinery_correction"]
    assert found["holdout"] == {"protocol": before["protocol"], "computed_before_correction": True}


def test_the_report_puts_the_old_and_new_stitched_curve_and_fresh_period_side_by_side(
    reports: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    before, after, markdown = reports
    found = after["machinery_correction"]

    assert found["corrects"] == before["protocol"]
    assert found["description"] == DESCRIPTION
    assert found["evidence"] == {
        "test": EVIDENCE.test,
        "record": "contract_note",
        "citation": EVIDENCE.citation,
        "failed_at": EVIDENCE.failed_at,
        "failed": FAILED.summary,
        "passed": PASSED.summary,
    }
    stitched = found["stitched_curve"]
    metrics = ("trades", "sharpe", "max_drawdown", "profit_factor")
    assert stitched["before"] == {m: before["stitched_curve"]["metrics"][m] for m in metrics}
    assert stitched["after"] == {m: after["stitched_curve"]["metrics"][m] for m in metrics}
    # STT at twice the rate changes every trade's money: the correction moved the curve.
    assert stitched["after"] != stitched["before"]
    fresh = found["fresh_period"]
    assert fresh["from"] == str(FRESH_FIRST)
    assert fresh["through"] == before["gate"]["standing"]["fresh_period"]["last"]
    assert fresh["before"]["trades"] == fresh["after"]["trades"] == 3
    assert D(fresh["after"]["sharpe"]) < D(fresh["before"]["sharpe"])
    assert "## Machinery Correction" in markdown
    assert "| Stitched Curve | profit_factor |" in markdown


def test_the_holdout_is_flagged_sensitive_exactly_where_its_margin_is_under_the_effect(
    reports: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    _, after, markdown = reports
    found = after["machinery_correction"]
    holdout = {c["metric"]: c for c in after["gate"]["checks"] if c["curve"] == "holdout"}
    stitched = found["stitched_curve"]

    assert [c["metric"] for c in found["sensitivity"]] == list(holdout)
    for check in found["sensitivity"]:
        gated = holdout[check["metric"]]
        margin, effect = D(check["holdout_margin"]), D(check["effect"])
        # The margin is the Holdout's distance from its threshold; the effect the Stitched
        # Curve's move from before to after, against the same fixed threshold.
        assert abs(margin - abs(D(gated["value"]) - D(gated["threshold"]))) < D("0.0002")
        moved = abs(D(stitched["after"][check["metric"]]) - D(stitched["before"][check["metric"]]))
        assert abs(effect - moved) < D("0.0002")
        assert check["sensitive"] is (margin < effect)
    assert found["sensitive"] is any(c["sensitive"] for c in found["sensitivity"])
    assert ("sensitive to this correction" in markdown) is found["sensitive"]


def test_the_fresh_period_continues_under_the_correction_from_the_same_first_day(
    corrected: tuple[str, str, str],
) -> None:
    url, original, correction = corrected

    with app_connection(url) as conn:
        before = queries.fresh_period_run(conn, original)
        after = queries.fresh_period_run(conn, correction)
        assert before is not None and after is not None
        old = queries.runs(conn, mode="backtest", backtest_id=before.backtest_id)
        new = queries.runs(conn, mode="backtest", backtest_id=after.backtest_id)

    assert after.backtest_id != before.backtest_id
    assert after.first == before.first == FRESH_FIRST
    assert after.parameters == before.parameters
    # Rerun from its first day at the fixed commit, over the days the old account ran.
    assert [r.as_of for r in new] == [r.as_of for r in old]
    assert {r.strategy.protocol for r in new} == {correction}


def cli(url: str, directory: Path, *arguments: str) -> tuple[int, str]:
    config = directory / "tradeline.toml"
    config.write_text(config_text(gate_market()))
    out = io.StringIO()
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(out):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        code = main(list(arguments))
    return code, out.getvalue()


def test_protocol_freeze_refuses_a_correction_without_all_of_its_evidence(
    later: tuple[str, str], tmp_path: Path
) -> None:
    url, protocol = later

    code, logged = cli(
        url, tmp_path, "protocol", "freeze", "--corrects", protocol, "--record", "contract_note"
    )

    assert code == 1
    assert "--correction, --test, --citation, --failed-at" in logged
    with app_connection(url) as conn:
        assert len(queries.protocols(conn)) == 2
