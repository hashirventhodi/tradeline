"""The Holdout's sensitivity to a Machinery Correction (ADR 0007 amendment), on hand-built
curves, no database.

After a correction the Holdout's recorded result stands. Beside it the report puts the
correction's measured effect on the Stitched Curve: how far each gated check's value moved
against its threshold between the curve before and after the correction. The Holdout is
flagged sensitive when its own margin against some gated threshold (its distance from it,
on whichever side) is smaller than that effect on the same check. Each number is worked by
hand from the curves below.
"""

from decimal import Decimal

from tradeline.gate import BenchmarkCurve, CurveMetrics, correction_sensitivity
from tradeline.protocol import BenchmarkGate, Gate

D = Decimal
BENCHMARK_GATE = BenchmarkGate(
    excess_above=D(0), drawdown_margin=D("0.02"), min_stitched_rebalances=8
)
PULLBACK_GATE = Gate(D("0.5"), D("0.15"), D("1.15"), 7)


def _curve(excess: str, drawdown: str, benchmark_drawdown: str = "0.12") -> BenchmarkCurve:
    return BenchmarkCurve(
        rebalances=10,
        excess=D(excess),
        drawdown=D(drawdown),
        benchmark_drawdown=D(benchmark_drawdown),
        information_ratio=D("0.4"),
    )


# Before the correction the Stitched Curve beat the Benchmark by 3 points a year with a 10
# percent drawdown; after it, by 2.5 points with 10.4 percent: the correction moved the
# excess by 0.005 and the drawdown by 0.004 against the same Benchmark drawdown.
BEFORE = _curve("0.03", "0.10")
AFTER = _curve("0.025", "0.104")


def _by_metric(holdout: BenchmarkCurve) -> dict[str, tuple[Decimal | None, Decimal | None, bool]]:
    found = correction_sensitivity(BENCHMARK_GATE, before=BEFORE, after=AFTER, holdout=holdout)
    return {s.metric: (s.margin, s.effect, s.sensitive) for s in found.checks}


def test_a_holdout_clear_of_every_threshold_by_more_than_the_effect_is_not_sensitive() -> None:
    # Excess 0.02 above zero (margin 0.02 > 0.005); drawdown 0.11 against 0.12 + 0.02 = 0.14
    # (margin 0.03 > 0.004).
    holdout = _curve("0.02", "0.11")

    found = correction_sensitivity(BENCHMARK_GATE, before=BEFORE, after=AFTER, holdout=holdout)

    assert _by_metric(holdout) == {
        "annualised_excess": (D("0.02"), D("0.005"), False),
        "drawdown": (D("0.03"), D("0.004"), False),
    }
    assert found.sensitive is False


def test_an_excess_margin_smaller_than_the_corrections_effect_on_excess_flags_it() -> None:
    # Excess 0.004 above zero: inside the correction's 0.005.
    holdout = _curve("0.004", "0.11")

    found = correction_sensitivity(BENCHMARK_GATE, before=BEFORE, after=AFTER, holdout=holdout)

    assert _by_metric(holdout)["annualised_excess"] == (D("0.004"), D("0.005"), True)
    assert found.sensitive is True
    assert found.sensitive_to == ("annualised_excess",)


def test_a_failing_holdout_close_to_its_threshold_is_sensitive_too() -> None:
    # Drawdown 0.143 against at most 0.14: failing by 0.003, inside the effect of 0.004.
    holdout = _curve("0.02", "0.143")

    found = correction_sensitivity(BENCHMARK_GATE, before=BEFORE, after=AFTER, holdout=holdout)

    assert _by_metric(holdout)["drawdown"] == (D("0.003"), D("0.004"), True)
    assert found.sensitive_to == ("drawdown",)


def test_a_margin_equal_to_the_effect_is_not_smaller_so_not_flagged() -> None:
    holdout = _curve("0.005", "0.136")

    found = correction_sensitivity(BENCHMARK_GATE, before=BEFORE, after=AFTER, holdout=holdout)

    assert _by_metric(holdout) == {
        "annualised_excess": (D("0.005"), D("0.005"), False),
        "drawdown": (D("0.004"), D("0.004"), False),
    }
    assert found.sensitive is False


def test_the_drawdown_effect_is_against_its_threshold_so_a_moved_benchmark_counts() -> None:
    # The correction moved the Benchmark's drawdown by 0.01 too (0.12 -> 0.13), and the
    # Strategy's by 0.004: the threshold moved 0.01, the value 0.004, the margin 0.006.
    after = _curve("0.025", "0.104", benchmark_drawdown="0.13")
    holdout = _curve("0.02", "0.135")  # 0.005 under 0.14

    found = correction_sensitivity(BENCHMARK_GATE, before=BEFORE, after=after, holdout=holdout)

    (drawdown,) = (s for s in found.checks if s.metric == "drawdown")
    assert (drawdown.margin, drawdown.effect, drawdown.sensitive) == (
        D("0.005"),
        D("0.006"),
        True,
    )


def test_under_pullbacks_gate_each_holdout_threshold_is_compared_with_its_own_effect() -> None:
    before = CurveMetrics(trades=7, sharpe=D("1.0"), max_drawdown=D("0.05"), profit_factor=D("1.4"))
    after = CurveMetrics(trades=7, sharpe=D("0.9"), max_drawdown=D("0.06"), profit_factor=D("1.3"))
    # Sharpe 0.55 is 0.05 above 0.5, inside the 0.1 effect; drawdown 0.10 is 0.05 under
    # 0.15, outside the 0.01; profit factor 2 is 0.85 above 1.15, outside the 0.1.
    holdout = CurveMetrics(trades=3, sharpe=D("0.55"), max_drawdown=D("0.10"), profit_factor=D("2"))

    found = correction_sensitivity(PULLBACK_GATE, before=before, after=after, holdout=holdout)

    assert [(s.metric, s.margin, s.effect, s.sensitive) for s in found.checks] == [
        ("sharpe", D("0.05"), D("0.1"), True),
        ("max_drawdown", D("0.05"), D("0.01"), False),
        ("profit_factor", D("0.85"), D("0.1"), False),
    ]
    assert found.sensitive_to == ("sharpe",)


def test_a_metric_a_curve_does_not_give_has_no_measured_margin_and_is_not_flagged() -> None:
    # No Sharpe on the Holdout, and an unbounded profit factor before the correction.
    before = CurveMetrics(
        trades=7, sharpe=D("1.0"), max_drawdown=D("0.05"), profit_factor=D("Infinity")
    )
    after = CurveMetrics(trades=7, sharpe=D("0.9"), max_drawdown=D("0.05"), profit_factor=D("2"))
    holdout = CurveMetrics(trades=3, sharpe=None, max_drawdown=D("0.10"), profit_factor=D("1.2"))

    found = correction_sensitivity(PULLBACK_GATE, before=before, after=after, holdout=holdout)

    assert [(s.metric, s.margin, s.effect, s.sensitive) for s in found.checks] == [
        ("sharpe", None, D("0.1"), False),
        ("max_drawdown", D("0.05"), D("0"), False),
        ("profit_factor", D("0.05"), None, False),
    ]
    assert found.sensitive is False
