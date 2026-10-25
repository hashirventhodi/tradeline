"""The Backtest Gate and the full report: ``tradeline backtest report --protocol P`` once a
Protocol's Walk-Forward and Holdout have run, read back through its JSON, its Markdown and
``tradeline.queries``.

The market is ``synthetic_eod.walk_forward_market`` (see ``tests/test_walk_forward.py``)
with three pullbacks added in the Holdout, 2023-01, where the version runs at k = 2, r = 2,
n = 5: BIRCH's on 2023-01-04 and ASH's on 2023-01-18 rise to their time stops, and
CEDAR's on 2023-01-11 is followed by a flat week (slope 0 from 2023-01-12, rising again
from 2023-01-20), so it sells at its entry and loses its charges. None of it touches the
Windows, so the Stitched Curve is the one ``test_walk_forward.py`` works.

- Stitched Curve: seven closed trades netting -1,133.77, 775.20, -1,222.21, 1,065.22,
  368.76, 438.85 and 89.32 (the Boundary Exit), so its profit factor is 2,737.35 /
  2,355.98 = 1.1619; its deepest fall is 0.004069 under its peak.
- Holdout: BIRCH 101 shares 739 -> 745 nets 333.80, CEDAR 113 at 664 -> 664 nets -274.11
  (both sides' charges), ASH 88 shares 849 -> 855 nets 257.75: profit factor 591.55 /
  274.11 = 2.1581, three trades.

The market runs on to 2024-02-29, past the Holdout, for the Fresh Period: three more
pullbacks (BIRCH 2023-03-01, ASH 2023-06-01, CEDAR 2023-09-01), each rising to its time
stop, so at k = 2, r = 2, n = 5 a Fresh Period from 2023-02-01 closes three winning trades
and none losing (an unbounded profit factor). Nothing after 2023-01-31 is read by the
Windows or the Holdout.

The fixture Protocol's Gate is lowered to fit them (Sharpe above 0.5, drawdown under 15
percent, profit factor above 1.15, at least 7 Stitched Curve trades); the failing one
asks a profit factor above 1.2, which the Stitched Curve misses and the Holdout clears.
The Halts are lowered to a 0.2 percent daily loss and a 0.4 percent drawdown, so the
drawdown Halt fires on the Stitched Curve's deepest days and never on the Holdout.
"""

import contextlib
import io
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from limits import PULLBACK
from synthetic_eod import (
    REPO_ROOT,
    WALK_FORWARD_RANGE,
    Dip,
    Market,
    Trend,
    compute_universe_months,
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
from tradeline.db import app_connection, connect
from tradeline.holdout import run_holdout
from tradeline.metrics import HaltLimits
from tradeline.protocol import Gate
from tradeline.protocol_report import read_protocol_report, report_protocol
from tradeline.strategy import Strategy
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes
from tradeline.walk_forward import walk_forward

D = Decimal
CALENDAR = repo_calendar()
TICK_SIZES = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
LIMITS = replace(PULLBACK, max_sector=D(1))
HALTS = HaltLimits(daily_loss=D("0.002"), drawdown=D("0.004"))
PASSING = Gate(D("0.5"), D("0.15"), D("1.15"), 7)
FAILING = replace(PASSING, profit_factor_above=D("1.2"))
# The first version's grid without k = 3, which no Window chose.
SAME_CHOICES = {"k": (D(2),), "r": (D(2),), "n": (D(5), D(10))}


FRESH_FIRST = date(2023, 2, 1)
FRESH_END = date(2024, 2, 29)


def gate_market() -> Market:
    pullback = D("17.00")
    return (
        replace(walk_forward_market(), end=FRESH_END)
        .with_events("BIRCH", Dip(date(2023, 1, 4), pullback), Dip(date(2023, 3, 1), pullback))
        .with_events(
            "CEDAR",
            Dip(date(2023, 1, 11), pullback),
            Trend(date(2023, 1, 12), D(0)),
            Trend(date(2023, 1, 20), D("1.00")),
        )
        .with_events("CEDAR", Dip(date(2023, 9, 1), pullback))
        .with_events("ASH", Dip(date(2023, 1, 18), pullback), Dip(date(2023, 6, 1), pullback))
    )


def pullback(parameters: Mapping[str, Decimal]) -> Strategy:
    return PullbackV1(k=parameters["k"], r=parameters["r"], n=int(parameters["n"]))


@pytest.fixture(scope="module")
def loaded(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    """The market loaded, nothing frozen: each Protocol below starts from a copy."""
    market = gate_market()
    fixtures = write_fixtures(market, tmp_path_factory.mktemp("gate"), CALENDAR)
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
    return database_url


def run_version(url: str, gate: Gate, grid: Any = None) -> str:
    """A version frozen under ``gate``, walked forward and run on the Holdout: its hash."""
    terms = replace(walk_forward_terms(), gate=gate)
    if grid is not None:
        terms = replace(terms, grid=grid)
    with app_connection(url) as conn:
        found = freeze_protocol(
            conn, gate_market(), *WALK_FORWARD_RANGE, terms=terms, limits=LIMITS, halts=HALTS
        ).hash
        walk_forward(conn, pullback, protocol=found, calendar=CALENDAR, tick_sizes=TICK_SIZES)
        run_holdout(conn, pullback, protocol=found, calendar=CALENDAR, tick_sizes=TICK_SIZES)
    return found


def report_command(url: str, protocol: str, directory: Path) -> tuple[dict[str, Any], str]:
    """``backtest report --protocol``: its JSON and its Markdown."""
    config = directory / "tradeline.toml"
    config.write_text(config_text(gate_market()))
    out, markdown = directory / "report.json", directory / "report.md"
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        arguments = ["--protocol", protocol, "--out", str(out), "--markdown", str(markdown)]
        assert main(["backtest", "report", *arguments]) == 0
    found: dict[str, Any] = json.loads(out.read_text())
    return found, markdown.read_text()


Reported = tuple[str, str, dict[str, Any], str]


@pytest.fixture(scope="module")
def passing(
    loaded: str, clone_database: Callable[[str], str], tmp_path_factory: pytest.TempPathFactory
) -> Reported:
    url = clone_database(loaded)
    protocol = run_version(url, PASSING)
    report, markdown = report_command(url, protocol, tmp_path_factory.mktemp("passing"))
    return url, protocol, report, markdown


def test_the_fixture_protocols_gate_is_recorded_with_its_inputs_and_thresholds(
    passing: Reported,
) -> None:
    url, protocol, report, _ = passing

    with app_connection(url) as conn:
        (evaluation,) = queries.gate_evaluations(conn, protocol=protocol)
        stitched = queries.walk_forward(conn, protocol)
        (attempt,) = queries.holdout_attempts(conn, protocol=protocol)

    assert evaluation.outcome == "pass"
    assert stitched is not None and evaluation.stitched_backtest_id == stitched.backtest_id
    assert evaluation.holdout_attempt == attempt.id
    assert evaluation.thresholds == {
        "sharpe_above": "0.5",
        "drawdown_below": "0.15",
        "profit_factor_above": "1.15",
        "min_stitched_trades": 7,
    }
    inputs = evaluation.inputs
    assert (inputs["stitched_curve"]["trades"], inputs["holdout"]["trades"]) == (7, 3)
    assert D(inputs["stitched_curve"]["profit_factor"]).quantize(D("0.0001")) == D("1.1619")
    assert D(inputs["holdout"]["profit_factor"]).quantize(D("0.0001")) == D("2.1581")
    assert evaluation.standing["independent"] is True
    assert evaluation.contract_notes == 0
    assert all(c["passed"] for c in evaluation.checks)
    # The report states the outcome as recorded.
    gate = report["gate"]
    assert (gate["outcome"], gate["holdout_trades"]) == ("pass", 3)
    assert gate["thresholds"] == evaluation.thresholds


def test_a_protocol_arranged_to_miss_one_threshold_fails_the_gate(
    loaded: str, clone_database: Callable[[str], str], tmp_path: Path
) -> None:
    url = clone_database(loaded)
    protocol = run_version(url, FAILING)

    report, markdown = report_command(url, protocol, tmp_path)

    gate = report["gate"]
    assert gate["outcome"] == "fail"
    missed = [(c["curve"], c["metric"]) for c in gate["checks"] if not c["passed"]]
    assert missed == [("stitched_curve", "profit_factor")]
    assert "stitched_curve profit_factor" in gate["reason"]
    assert "FAIL" in markdown.split("\n## ")[1]


def test_the_holdout_trade_count_is_reported_and_not_gated(passing: Reported) -> None:
    gate = passing[2]["gate"]

    # Three Holdout trades against a minimum of seven: the minimum is the Stitched Curve's.
    assert gate["holdout_trades"] == 3
    assert not any(c["curve"] == "holdout" and c["metric"] == "trades" for c in gate["checks"])
    assert gate["outcome"] == "pass"


def test_a_not_independent_run_without_a_fresh_period_cannot_pass_whatever_its_metrics(
    passing: Reported, clone_database: Callable[[str], str], tmp_path: Path
) -> None:
    url = clone_database(passing[0])
    # A later version of the family, frozen now on a smaller grid that makes the same choices:
    # its Holdout run is Not Independent, and no data lies after both the Holdout and today.
    later = run_version(url, PASSING, grid=SAME_CHOICES)

    report, markdown = report_command(url, later, tmp_path)

    gate = report["gate"]
    assert gate["outcome"] == "cannot pass"
    # Every threshold holds, as the first version's did; the standing alone decides.
    assert report["stitched_curve"]["metrics"] == passing[2]["stitched_curve"]["metrics"]
    assert all(c["passed"] for c in gate["checks"])
    assert gate["standing"]["independent"] is False
    assert gate["standing"]["may_pass"] is False
    assert "twelve months" in gate["reason"]
    assert "CANNOT PASS" in markdown.split("\n## ")[1]
    assert report["lineage"]["consumed_by"] == passing[1]


def fresh_command(url: str, protocol: str, directory: Path) -> tuple[int, str]:
    """``backtest fresh --protocol``: its exit code and what it logged."""
    config = directory / "tradeline.toml"
    config.write_text(config_text(gate_market()))
    out = io.StringIO()
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(out):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        code = main(["backtest", "fresh", "--protocol", protocol])
    return code, out.getvalue()


def frozen_on_the_holdouts_last_day(url: str, *protocols: str) -> None:
    """Stand in for versions frozen on 2023-01-31, in order, as history this suite cannot
    replay: the owner's connection moves their freeze times, as no application role can."""
    with connect(url) as conn:
        for hour, protocol in enumerate(protocols, start=10):
            conn.execute(
                "UPDATE protocols SET frozen_at = %s WHERE hash = %s",
                (datetime(2023, 1, 31, hour, tzinfo=IST), protocol),
            )


def test_a_not_independent_run_passes_with_twelve_fresh_months_that_clear_every_threshold(
    passing: Reported, clone_database: Callable[[str], str], tmp_path: Path
) -> None:
    url = clone_database(passing[0])
    later = run_version(url, PASSING, grid=SAME_CHOICES)
    frozen_on_the_holdouts_last_day(url, passing[1], later)
    with app_connection(url) as conn:
        compute_universe_months(conn, gate_market(), FRESH_FIRST, FRESH_END)

    code, _ = fresh_command(url, later, tmp_path)
    report, markdown = report_command(url, later, tmp_path)

    assert code == 0
    gate = report["gate"]
    assert gate["standing"]["independent"] is False
    assert gate["standing"]["fresh_period"]["first"] == "2023-02-01"
    assert gate["standing"]["fresh_period"]["long_enough"] is True
    fresh = [c for c in gate["checks"] if c["curve"] == "fresh_period"]
    assert [(c["metric"], c["passed"]) for c in fresh] == [
        ("sharpe", True),
        ("max_drawdown", True),
        ("profit_factor", True),
    ]
    assert fresh[2]["value"] == "∞"
    assert gate["fresh_period_trades"] == 3
    assert gate["outcome"] == "pass"
    assert "| Fresh Period | profit_factor | ∞ |" in markdown
    assert "The Fresh Period closed 3 trades." in markdown


def test_a_fresh_period_is_refused_to_a_version_whose_holdout_run_is_independent(
    passing: Reported, tmp_path: Path
) -> None:
    code, logged = fresh_command(passing[0], passing[1], tmp_path)

    assert code == 1
    assert "independent" in logged


def test_the_markdown_puts_the_gate_and_the_halt_days_together_at_the_top_then_every_section(
    passing: Reported,
) -> None:
    markdown = passing[3]

    sections = re.findall(r"^## (.+)$", markdown, flags=re.MULTILINE)
    assert sections[:2] == ["Backtest Gate: PASS", "Halt would have fired"]
    assert sections[2:] == [
        "Windows",
        "Stitched Curve and Holdout",
        "Gapped Stops",
        "Limits: cap-binding and Unclassified",
        "Cost sensitivity",
        "Universe months",
        "Lineage",
        "Contract notes",
        "Runtime",
    ]
    assert "pending" in markdown.split("## Contract notes")[1].split("##")[0]


def test_the_report_lists_halt_days_windows_universe_months_lineage_and_c_sensitivity(
    passing: Reported,
) -> None:
    url, protocol, report, _ = passing

    halts = report["halt_would_have_fired"]
    assert (halts["daily_loss_limit"], halts["drawdown_limit"]) == ("0.002000", "0.004000")
    assert halts["holdout"] == []
    assert halts["stitched_curve"]
    assert max(D(d["drawdown"]) for d in halts["stitched_curve"]) == D("0.004069")
    assert all("drawdown" in d["limits"] for d in halts["stitched_curve"])
    assert list(report)[:4] == ["protocol", "strategy", "gate", "halt_would_have_fired"]

    assert [w["selection"] and w["selection"]["parameters"] for w in report["windows"]] == [
        None,
        {"k": "2", "n": "10", "r": "2"},
        {"k": "2", "n": "5", "r": "2"},
    ]
    assert report["stitched_curve"]["metrics"]["profit_factor"] == "1.1619"
    assert report["holdout"]["metrics"]["profit_factor"] == "2.1581"
    assert report["holdout"]["parameters"] == {"k": "2", "n": "5", "r": "2"}
    assert report["gapped_stops"] == {"stitched_curve": [], "holdout": []}
    assert report["limits"]["holdout"]["unclassified"] == {"blocks": 0, "trades": 3}

    months = report["universe_months"]
    assert [m["month"] for m in months][0::9] == ["2022-04-01", "2023-01-01"]
    assert len(months) == 10 and {m["mode"] for m in months} == {"recorded"}

    lineage = report["lineage"]
    assert (lineage["family"], lineage["consumed_by"]) == ("pullback", protocol)
    (version,) = lineage["versions"]
    assert version["protocol"] == protocol
    assert (version["holdout"]["consumes"], version["holdout"]["independent"]) == (True, True)

    sensitivity = report["cost_sensitivity"]
    assert sensitivity["impact"] == "0.1"
    zero, double = sensitivity["variants"]
    assert (zero["impact"], double["impact"]) == ("0", "0.2")
    for curve in ("stitched_curve", "holdout"):
        ending = [
            D(x["ending_equity"]) for x in (zero[curve], report[curve]["metrics"], double[curve])
        ]
        assert ending == sorted(ending, reverse=True) and len(set(ending)) == 3
    with app_connection(url) as conn:
        accounts = queries.cost_sensitivities(conn, protocol)
        impacts = {
            (a.curve, a.impact): {
                r.impact for r in queries.runs(conn, mode="backtest", backtest_id=a.backtest_id)
            }
            for a in accounts
        }
    assert impacts == {
        ("stitched_curve", D(0)): {D(0)},
        ("holdout", D(0)): {D(0)},
        ("stitched_curve", D("0.2")): {D("0.2")},
        ("holdout", D("0.2")): {D("0.2")},
    }
    assert report["contract_notes"]["status"] == "pending"


def test_the_report_is_a_reading_of_the_ledger_rendered_twice_the_same(
    passing: Reported, tmp_path: Path
) -> None:
    url, protocol, report, markdown = passing

    with app_connection(url) as conn:
        first, second = read_protocol_report(conn, protocol), read_protocol_report(conn, protocol)
        runs_before = len(queries.runs(conn, mode="backtest"))

    assert first.text() == second.text() == json.dumps(report, indent=2) + "\n"
    assert first.markdown() == second.markdown() == markdown
    # The command again evaluates the Gate again, runs nothing again, and reads the same.
    again, _ = report_command(url, protocol, tmp_path)
    with app_connection(url) as conn:
        assert len(queries.gate_evaluations(conn, protocol=protocol)) == 2
        assert len(queries.runs(conn, mode="backtest")) == runs_before
    assert again == report


def test_the_report_is_refused_before_the_holdout_has_run(
    loaded: str, clone_database: Callable[[str], str]
) -> None:
    url = clone_database(loaded)
    with app_connection(url) as conn:
        found = freeze_protocol(
            conn,
            gate_market(),
            *WALK_FORWARD_RANGE,
            terms=replace(walk_forward_terms(), gate=PASSING),
            limits=LIMITS,
            halts=HALTS,
        ).hash
        walk_forward(conn, pullback, protocol=found, calendar=CALENDAR, tick_sizes=TICK_SIZES)

        with pytest.raises(ValueError, match="has not run the Holdout"):
            report_protocol(
                conn,
                pullback,
                protocol=found,
                calendar=CALENDAR,
                tick_sizes=TICK_SIZES,
                contract_notes=0,
            )
        assert queries.gate_evaluations(conn, protocol=found) == []


@pytest.mark.parametrize("table", ["gate_evaluations", "cost_sensitivities", "fresh_periods"])
def test_the_application_role_cannot_change_or_remove_an_evaluation_a_sensitivity_or_a_fresh_period(
    passing: Reported, table: str
) -> None:
    with app_connection(passing[0]) as conn:
        for statement in (f"UPDATE {table} SET protocol = protocol", f"DELETE FROM {table}"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
