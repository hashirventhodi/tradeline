"""The Backtest Gate's rule (``tradeline.gate.evaluate``) on fixed metrics, no database: Sharpe
above 1.0, drawdown under 15 percent and profit factor above 1.3 on both the Stitched Curve
and the Holdout, at least 150 trades on the Stitched Curve, the Holdout's count reported.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from tradeline.gate import CurveMetrics, evaluate
from tradeline.protocol import Gate

D = Decimal
GATE = Gate(D("1.0"), D("0.15"), D("1.3"), 150)
GOOD = CurveMetrics(trades=150, sharpe=D("1.01"), max_drawdown=D("0.149"), profit_factor=D("1.31"))
HOLDOUT = replace(GOOD, trades=3)


def independent(stitched: CurveMetrics, holdout: CurveMetrics) -> str:
    return evaluate(
        GATE, stitched=stitched, holdout=holdout, independent=True, may_pass=True, standing="ok"
    ).outcome


def test_the_gate_passes_when_every_threshold_holds_on_both_curves_however_few_holdout_trades() -> (
    None
):
    found = evaluate(
        GATE, stitched=GOOD, holdout=HOLDOUT, independent=True, may_pass=True, standing="ok"
    )

    assert found.outcome == "pass"
    assert found.holdout_trades == 3
    assert all(c.passed for c in found.checks)
    assert [(c.curve, c.metric) for c in found.checks] == [
        ("stitched_curve", "sharpe"),
        ("stitched_curve", "max_drawdown"),
        ("stitched_curve", "profit_factor"),
        ("stitched_curve", "trades"),
        ("holdout", "sharpe"),
        ("holdout", "max_drawdown"),
        ("holdout", "profit_factor"),
    ]


@pytest.mark.parametrize(
    ("curve", "change"),
    [
        ("stitched", {"sharpe": D("1.0")}),
        ("stitched", {"max_drawdown": D("0.15")}),
        ("stitched", {"profit_factor": D("1.3")}),
        ("stitched", {"trades": 149}),
        ("holdout", {"sharpe": D("0.99")}),
        ("holdout", {"max_drawdown": D("0.2")}),
        ("holdout", {"profit_factor": D("1.29")}),
        # A metric the curve does not give (no varying return, no losing trade) is not
        # established, so it does not clear its threshold.
        ("stitched", {"sharpe": None}),
        ("holdout", {"profit_factor": None}),
    ],
)
def test_one_threshold_missed_on_either_curve_fails_the_gate(
    curve: str, change: dict[str, object]
) -> None:
    stitched = replace(GOOD, **change) if curve == "stitched" else GOOD  # type: ignore[arg-type]
    holdout = replace(HOLDOUT, **change) if curve == "holdout" else HOLDOUT  # type: ignore[arg-type]

    found = evaluate(
        GATE, stitched=stitched, holdout=holdout, independent=True, may_pass=True, standing="ok"
    )

    assert found.outcome == "fail"
    (missed,) = [c for c in found.checks if not c.passed]
    assert missed.curve == ("stitched_curve" if curve == "stitched" else "holdout")
    assert missed.metric in found.reason


def test_the_holdout_trade_count_never_changes_the_outcome() -> None:
    assert {independent(GOOD, replace(HOLDOUT, trades=n)) for n in (0, 1, 500)} == {"pass"}


def test_a_not_independent_run_without_a_fresh_period_cannot_pass_whatever_its_metrics() -> None:
    found = evaluate(
        GATE,
        stitched=GOOD,
        holdout=HOLDOUT,
        independent=False,
        may_pass=False,
        standing="Not Independent: its Fresh Period is short of twelve months",
    )

    assert found.outcome == "cannot pass"
    assert "Fresh Period is short" in found.reason
    # Its metrics are still scored and recorded beside the outcome.
    assert all(c.passed for c in found.checks)


def test_a_not_independent_run_passes_only_with_a_fresh_period_on_the_same_thresholds() -> None:
    def outcome(fresh: CurveMetrics | None) -> str:
        return evaluate(
            GATE,
            stitched=GOOD,
            holdout=HOLDOUT,
            independent=False,
            may_pass=True,
            standing="Not Independent: twelve months of Fresh Period",
            fresh_period=fresh,
        ).outcome

    assert outcome(None) == "cannot pass"
    assert outcome(replace(GOOD, trades=2)) == "pass"
    assert outcome(replace(GOOD, sharpe=D("0.5"))) == "fail"
