"""The Backtest Gate against the Benchmark (ADR 0015 amendment) on hand-built curves, no
database: annualised excess of Liquidation Value over the Benchmark's above zero and
drawdown of Liquidation Value no worse than the Benchmark's plus 2 points, on both the
Stitched Curve and the Holdout; at least 8 Rebalances on the Stitched Curve.

The curves are worked by hand. 504 trading days are two years: a Strategy ending at 1.21
times its start returned 10 percent a year, a Benchmark ending at 1.1025 times 5 percent.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tradeline.benchmark import UnitSplit, chained_navs
from tradeline.gate import BenchmarkCurve, GateOutcome, benchmark_curve, evaluate
from tradeline.metrics import benchmark_comparison
from tradeline.protocol import BenchmarkGate, gate_content, read_gate

D = Decimal
START = D(500000)
GATE = BenchmarkGate(excess_above=D(0), drawdown_margin=D("0.02"), min_stitched_rebalances=8)


def _two_years(end: Decimal, *, peak: Decimal, trough: Decimal) -> list[Decimal]:
    """504 days of values from ``START``: up to ``peak`` times it, down to ``trough``
    times it, then flat until the last day ends at ``end`` times it."""
    values = [START * peak, START * trough] + [START * trough] * 501 + [START * end]
    assert len(values) == 504
    return values


def _curve(
    strategy: list[Decimal], benchmark: list[Decimal], rebalances: int = 9
) -> BenchmarkCurve:
    return benchmark_curve(
        rebalances=rebalances, comparison=benchmark_comparison(strategy, benchmark, start=START)
    )


# The Strategy: 10 percent a year, a 10 percent drawdown (1.25 to 1.125). The Benchmark: 5
# percent a year, a 12 percent drawdown (1.25 to 1.10).
STRATEGY = _two_years(D("1.21"), peak=D("1.25"), trough=D("1.125"))
BENCHMARK = _two_years(D("1.1025"), peak=D("1.25"), trough=D("1.10"))
GOOD = _curve(STRATEGY, BENCHMARK)


def _outcome(stitched: BenchmarkCurve, holdout: BenchmarkCurve) -> str:
    return evaluate(
        GATE, stitched=stitched, holdout=holdout, independent=True, may_pass=True, standing="ok"
    ).outcome


def test_the_comparison_annualises_each_return_over_252_trading_days_a_year() -> None:
    found = benchmark_comparison(STRATEGY, BENCHMARK, start=START)

    assert found.strategy_return == D("0.1")
    assert found.benchmark_return == D("0.05")
    assert found.excess == D("0.05")
    assert found.drawdown == D("0.1")
    assert found.benchmark_drawdown == D("0.12")


def test_the_information_ratio_is_the_mean_daily_active_return_over_its_deviation() -> None:
    # Active returns 0.01, 0, 0.01: mean 1/150, sample deviation 1/(100 sqrt 3), so the
    # ratio is (2/3) sqrt 3 a day and sqrt(336) a year.
    found = benchmark_comparison(
        [D(102), D(102), D("104.04")], [D(101), D(101), D("102.01")], start=D(100)
    )

    assert found.information_ratio is not None
    assert found.information_ratio.quantize(D("0.000001")) == D(336).sqrt().quantize(D("0.000001"))


def test_the_gate_passes_when_both_curves_beat_the_benchmark_within_its_drawdown() -> None:
    found = evaluate(
        GATE,
        stitched=GOOD,
        holdout=replace(GOOD, rebalances=3),
        independent=True,
        may_pass=True,
        standing="ok",
    )

    assert found.outcome == "pass"
    assert [(c.curve, c.metric, c.rule) for c in found.checks] == [
        ("stitched_curve", "annualised_excess", "above"),
        ("stitched_curve", "drawdown", "at most"),
        ("stitched_curve", "rebalances", "at least"),
        ("holdout", "annualised_excess", "above"),
        ("holdout", "drawdown", "at most"),
    ]
    # The drawdown's threshold is the Benchmark's drawdown plus 2 points.
    assert found.checks[1].threshold == D("0.14")
    # The Holdout's Rebalances are reported, not gated.
    assert found.holdout_count == 3


# Each threshold missed by a hand-built curve: no excess (both 5 percent a year); a
# drawdown just over the Benchmark's plus 2 points (14.1 against 12 + 2); and a drawdown
# exactly at it, which holds.
NO_EXCESS = _curve(BENCHMARK, BENCHMARK)
DEEPER = _curve(_two_years(D("1.21"), peak=D("1.25"), trough=D("1.07375")), BENCHMARK)
AT_MARGIN = _curve(_two_years(D("1.21"), peak=D("1.25"), trough=D("1.075")), BENCHMARK)


@pytest.mark.parametrize("curve", ["stitched", "holdout"])
@pytest.mark.parametrize(
    ("missed", "metric"), [(NO_EXCESS, "annualised_excess"), (DEEPER, "drawdown")]
)
def test_one_threshold_missed_on_either_curve_fails_the_gate(
    curve: str, missed: BenchmarkCurve, metric: str
) -> None:
    stitched = missed if curve == "stitched" else GOOD
    holdout = missed if curve == "holdout" else GOOD

    found = evaluate(
        GATE, stitched=stitched, holdout=holdout, independent=True, may_pass=True, standing="ok"
    )

    assert found.outcome == "fail"
    (failed,) = [c for c in found.checks if not c.passed]
    assert (failed.curve, failed.metric) == (
        "stitched_curve" if curve == "stitched" else "holdout",
        metric,
    )
    assert f"{failed.curve} {metric}" in found.reason


def test_a_drawdown_exactly_the_benchmarks_plus_2_points_holds() -> None:
    assert AT_MARGIN.drawdown == D("0.14")

    assert _outcome(AT_MARGIN, AT_MARGIN) == "pass"


def test_a_stitched_curve_with_7_rebalances_fails_the_rebalance_minimum() -> None:
    found = evaluate(
        GATE,
        stitched=replace(GOOD, rebalances=7),
        holdout=GOOD,
        independent=True,
        may_pass=True,
        standing="ok",
    )

    assert found.outcome == "fail"
    (failed,) = [c for c in found.checks if not c.passed]
    assert (failed.curve, failed.metric, failed.value, failed.threshold) == (
        "stitched_curve",
        "rebalances",
        7,
        8,
    )
    assert _outcome(replace(GOOD, rebalances=8), GOOD) == "pass"


def test_both_curves_failing_fails_and_names_each() -> None:
    found = evaluate(
        GATE, stitched=NO_EXCESS, holdout=DEEPER, independent=True, may_pass=True, standing="ok"
    )

    assert found.outcome == "fail"
    assert "stitched_curve annualised_excess" in found.reason
    assert "holdout drawdown" in found.reason


def test_a_not_independent_run_passes_only_with_a_fresh_period_beating_the_benchmark() -> None:
    def outcome(fresh: BenchmarkCurve | None) -> str:
        return evaluate(
            GATE,
            stitched=GOOD,
            holdout=GOOD,
            independent=False,
            may_pass=True,
            standing="Not Independent: twelve months of Fresh Period",
            fresh_period=fresh,
        ).outcome

    assert outcome(None) == "cannot pass"
    assert outcome(replace(GOOD, rebalances=2)) == "pass"
    assert outcome(NO_EXCESS) == "fail"


def test_a_fresh_period_with_one_rebalance_after_its_initial_buy_cannot_carry_the_gate() -> None:
    def scored(fresh: BenchmarkCurve) -> GateOutcome:
        return evaluate(
            GATE,
            stitched=GOOD,
            holdout=GOOD,
            independent=False,
            may_pass=True,
            standing="Not Independent: twelve months of Fresh Period",
            fresh_period=fresh,
        )

    one = scored(replace(GOOD, rebalances=1))
    assert one.outcome == "cannot pass"
    assert "1 Rebalance after its Initial Buy, short of 2" in one.reason
    (check,) = [c for c in one.checks if (c.curve, c.metric) == ("fresh_period", "rebalances")]
    assert (check.value, check.rule, check.threshold, check.passed) == (1, "at least", 2, False)
    # Short of its Rebalances, a Fresh Period that misses a threshold does not fail the Gate
    # either: it has not yet carried it.
    assert scored(replace(NO_EXCESS, rebalances=1)).outcome == "cannot pass"
    assert scored(replace(GOOD, rebalances=2)).outcome == "pass"
    assert scored(replace(NO_EXCESS, rebalances=2)).outcome == "fail"


def test_the_gate_freezes_the_fresh_periods_two_rebalances() -> None:
    assert GATE.min_fresh_period_rebalances == 2
    assert read_gate(gate_content(GATE)) == GATE


def test_a_unit_split_is_chained_so_the_fund_holds_its_value_across_it() -> None:
    # AMFI's NAVs of the ICICI Prudential Nifty 100 Low Vol 30 ETF around its unit split
    # of about 1 for 10 (docs/research.md): 196.363 on 2024-03-01, 19.7146 on 2024-03-04.
    split = UnitSplit(141596, date(2024, 3, 4), D(10), "AMFI's NAV history")
    navs = [
        (date(2024, 2, 29), D("195.000")),
        (date(2024, 3, 1), D("196.363")),
        (date(2024, 3, 4), D("19.7146")),
        (date(2024, 3, 5), D("19.8000")),
    ]

    assert chained_navs(navs, [split]) == [D("195.000"), D("196.363"), D("197.146"), D("198.000")]
