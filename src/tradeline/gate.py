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

A portfolio family's Gate is against the Benchmark (``BenchmarkGate``, ADR 0015
amendment of 2026-10-25): on both curves the annualised excess of Liquidation
Value over the Benchmark's is above the Protocol's (zero), and the drawdown of
Liquidation Value is at most the Benchmark's plus the Protocol's margin (2
points); the Stitched Curve holds at least the minimum number of Rebalances, and
the Holdout's are reported, never gated. The information ratio is read with them
and reported, never gated. A Not Independent run's Fresh Period clears the same
thresholds on its own curve.

After a Machinery Correction (ADR 0007 amendment) the Holdout's recorded outcome
stands; ``correction_sensitivity`` flags it sensitive to the correction when its
margin against a gated threshold is smaller than the correction's measured effect
on the Stitched Curve against the same threshold.

This module is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tradeline.metrics import Comparison
from tradeline.protocol import AnyGate, BenchmarkGate

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
class BenchmarkCurve:
    """What the Benchmark Gate reads of one curve: its Rebalances, the annualised excess of
    its Liquidation Value over the Benchmark's, each one's drawdown, and the information
    ratio (reported, never gated)."""

    rebalances: int
    excess: Decimal
    drawdown: Decimal
    benchmark_drawdown: Decimal
    information_ratio: Decimal | None


def benchmark_curve(*, rebalances: int, comparison: Comparison) -> BenchmarkCurve:
    """The curve as the Benchmark Gate reads it, from its Liquidation Values compared with the
    Benchmark's."""
    return BenchmarkCurve(
        rebalances=rebalances,
        excess=comparison.excess,
        drawdown=comparison.drawdown,
        benchmark_drawdown=comparison.benchmark_drawdown,
        information_ratio=comparison.information_ratio,
    )


AnyCurve = CurveMetrics | BenchmarkCurve


@dataclass(frozen=True)
class Check:
    """One threshold on one curve: the value read, the threshold, which side of it passes
    (``above``, ``below``, ``at least`` or ``at most``), and whether it holds."""

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
    # The Holdout's trades (a Benchmark Gate: its Rebalances), reported and never gated.
    holdout_count: int
    # The Fresh Period's, when one was scored.
    fresh_period_count: int | None
    reason: str


def evaluate(
    gate: AnyGate,
    *,
    stitched: AnyCurve,
    holdout: AnyCurve,
    independent: bool,
    may_pass: bool,
    standing: str,
    fresh_period: AnyCurve | None = None,
) -> GateOutcome:
    """The Gate on the Stitched Curve and the Holdout (and the Fresh Period, for a Not
    Independent run that ``may_pass`` with one); ``standing`` says why the Holdout run is
    or is not independent. Each curve is the kind its Gate reads."""
    checks = [*_checks(gate, "stitched_curve", stitched), *_checks(gate, "holdout", holdout)]
    checks.insert(len(checks) // 2, _minimum(gate, stitched))
    if not independent and may_pass and fresh_period is not None:
        checks += _checks(gate, "fresh_period", fresh_period)
    missed = [f"{c.curve} {c.metric}" for c in checks if not c.passed]
    outcome: Outcome
    if not independent and not may_pass:
        outcome, reason = "cannot pass", standing
    elif not independent and fresh_period is None:
        outcome = "cannot pass"
        reason = (
            f"{standing}; the Fresh Period has not been run over twelve months"
            " (`tradeline backtest fresh` runs it)"
        )
    elif missed:
        outcome, reason = "fail", "missed: " + ", ".join(missed)
    else:
        outcome, reason = (
            "pass",
            "every threshold holds on both curves"
            + ("" if independent else " and on the Fresh Period"),
        )
    return GateOutcome(
        outcome,
        tuple(checks),
        _count(holdout),
        None if fresh_period is None or independent else _count(fresh_period),
        reason,
    )


def _count(curve: AnyCurve) -> int:
    return curve.rebalances if isinstance(curve, BenchmarkCurve) else curve.trades


def _minimum(gate: AnyGate, stitched: AnyCurve) -> Check:
    """The Stitched Curve's minimum: its trades, or under a Benchmark Gate its Rebalances."""
    if isinstance(gate, BenchmarkGate):
        assert isinstance(stitched, BenchmarkCurve)
        metric, value, least = "rebalances", stitched.rebalances, gate.min_stitched_rebalances
    else:
        assert isinstance(stitched, CurveMetrics)
        metric, value, least = "trades", stitched.trades, gate.min_stitched_trades
    return Check("stitched_curve", metric, value, "at least", least, value >= least)


def _checks(gate: AnyGate, curve: str, metrics: AnyCurve) -> list[Check]:
    if isinstance(gate, BenchmarkGate):
        assert isinstance(metrics, BenchmarkCurve)
        most = metrics.benchmark_drawdown + gate.drawdown_margin
        return [
            Check(
                curve,
                "annualised_excess",
                metrics.excess,
                "above",
                gate.excess_above,
                metrics.excess > gate.excess_above,
            ),
            Check(curve, "drawdown", metrics.drawdown, "at most", most, metrics.drawdown <= most),
        ]
    assert isinstance(metrics, CurveMetrics)
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


@dataclass(frozen=True)
class CheckSensitivity:
    """One gated Holdout threshold after a Machinery Correction: the Holdout's ``margin``
    (its distance from the threshold, on whichever side), the correction's ``effect`` on
    the same check of the Stitched Curve (how far its value moved against its threshold),
    and whether the margin is the smaller. Either is None when a curve gives no finite
    value for it, and such a check is not flagged."""

    metric: str
    margin: Decimal | None
    effect: Decimal | None
    sensitive: bool


@dataclass(frozen=True)
class CorrectionSensitivity:
    checks: tuple[CheckSensitivity, ...]

    @property
    def sensitive_to(self) -> tuple[str, ...]:
        return tuple(c.metric for c in self.checks if c.sensitive)

    @property
    def sensitive(self) -> bool:
        return bool(self.sensitive_to)


def correction_sensitivity(
    gate: AnyGate, *, before: AnyCurve, after: AnyCurve, holdout: AnyCurve
) -> CorrectionSensitivity:
    """Whether the Holdout's recorded outcome is sensitive to a Machinery Correction (ADR
    0007 amendment): for each threshold the Gate holds the Holdout to, its margin against
    the correction's measured effect on the Stitched Curve, ``before`` and ``after`` it.
    The flag compares two measured numbers; it is never a threshold of its own."""
    moved: dict[str, Decimal] = {}
    for old, new in zip(
        _checks(gate, "stitched_curve", before), _checks(gate, "stitched_curve", after), strict=True
    ):
        was, now = _distance(old), _distance(new)
        if was is not None and now is not None:
            moved[old.metric] = abs(now - was)
    found = []
    for check in _checks(gate, "holdout", holdout):
        distance = _distance(check)
        margin = None if distance is None else abs(distance)
        effect = moved.get(check.metric)
        sensitive = margin is not None and effect is not None and margin < effect
        found.append(CheckSensitivity(check.metric, margin, effect, sensitive))
    return CorrectionSensitivity(tuple(found))


def _distance(check: Check) -> Decimal | None:
    """The check's value less its threshold; None when the value is not a finite number."""
    value = check.value
    if value is None or (isinstance(value, Decimal) and not value.is_finite()):
        return None
    return Decimal(value) - Decimal(check.threshold)
