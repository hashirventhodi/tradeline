"""The Backtest Gate's rule (ADR 0015, ``docs/risk-controls.md``): pass or fail as a fact.

The Gate passes when Sharpe is above its threshold, the max drawdown under its
threshold and the profit factor above its threshold on both the Stitched Curve
and the Holdout, and the Stitched Curve closed at least the minimum number of
trades; the Holdout's trades are reported, never gated. Either curve failing
fails it. A metric a curve does not give (Sharpe on a curve whose returns do
not vary, the profit factor with no closed trade) is not established, so it
does not clear its threshold; a profit factor with winners and no loser is
unbounded, and clears it. The thresholds are the Protocol's.

A Not Independent Holdout run (ADR 0007) cannot pass on its metrics alone: it
passes only together with a Fresh Period of at least twelve months that clears
the same thresholds (its trades reported). Without one long enough, or with one
not yet scored, the outcome is ``cannot pass``, whatever the metrics; they are
still scored and recorded beside it.

This module is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tradeline.protocol import Gate

Outcome = Literal["pass", "fail", "cannot pass"]


@dataclass(frozen=True)
class CurveMetrics:
    """What the Gate reads of one curve: its closed trades and its metrics (None when the
    curve does not give one)."""

    trades: int
    sharpe: Decimal | None
    max_drawdown: Decimal
    profit_factor: Decimal | None


@dataclass(frozen=True)
class Check:
    """One threshold on one curve: the value read, the threshold, which side of it passes
    (``above``, ``below`` or ``at least``), and whether the value is there."""

    curve: str
    metric: str
    value: Decimal | int | None
    rule: str
    threshold: Decimal | int
    passed: bool


@dataclass(frozen=True)
class GateOutcome:
    outcome: Outcome
    checks: tuple[Check, ...]
    holdout_trades: int
    # The Fresh Period's trades, when one was scored.
    fresh_period_trades: int | None
    reason: str


def evaluate(
    gate: Gate,
    *,
    stitched: CurveMetrics,
    holdout: CurveMetrics,
    independent: bool,
    may_pass: bool,
    standing: str,
    fresh_period: CurveMetrics | None = None,
) -> GateOutcome:
    """The Gate on the Stitched Curve and the Holdout (and the Fresh Period, for a Not
    Independent run that ``may_pass`` with one); ``standing`` says why the Holdout run is
    or is not independent."""
    checks = [*_checks(gate, "stitched_curve", stitched), *_checks(gate, "holdout", holdout)]
    checks.insert(
        3,
        Check(
            "stitched_curve",
            "trades",
            stitched.trades,
            "at least",
            gate.min_stitched_trades,
            stitched.trades >= gate.min_stitched_trades,
        ),
    )
    if not independent and may_pass and fresh_period is not None:
        checks += _checks(gate, "fresh_period", fresh_period)
    missed = [f"{c.curve} {c.metric}" for c in checks if not c.passed]
    outcome: Outcome
    if not independent and not may_pass:
        outcome, reason = "cannot pass", standing
    elif not independent and fresh_period is None:
        outcome = "cannot pass"
        reason = f"{standing}; the Fresh Period has not been scored"
    elif missed:
        outcome, reason = "fail", "missed: " + ", ".join(missed)
    else:
        outcome, reason = "pass", "every threshold holds on both curves"
    return GateOutcome(
        outcome,
        tuple(checks),
        holdout.trades,
        None if fresh_period is None or independent else fresh_period.trades,
        reason,
    )


def _checks(gate: Gate, curve: str, metrics: CurveMetrics) -> list[Check]:
    sharpe, drawdown, factor = metrics.sharpe, metrics.max_drawdown, metrics.profit_factor
    return [
        Check(
            curve,
            "sharpe",
            sharpe,
            "above",
            gate.sharpe_above,
            sharpe is not None and sharpe > gate.sharpe_above,
        ),
        Check(
            curve,
            "max_drawdown",
            drawdown,
            "below",
            gate.drawdown_below,
            drawdown < gate.drawdown_below,
        ),
        Check(
            curve,
            "profit_factor",
            factor,
            "above",
            gate.profit_factor_above,
            factor is not None and factor > gate.profit_factor_above,
        ),
    ]
