"""The metrics a backtest is scored by, as the Backtest Gate will score it (ADR 0015).

Pure arithmetic over the daily marked Equity curve (the starting Equity, then
each Run's Equity: cash plus the open Positions at the day's close) and the
closed trades' net P&L after costs (``docs/risk-controls.md``, Gates):

- Sharpe: the mean of the curve's daily excess returns over the risk-free rate
  (a yearly rate taken as rate / 252 a trading day; idle cash earns nothing),
  over their sample standard deviation, times the square root of 252.
- Max drawdown: the largest peak-to-trough fall of the curve, as a fraction of
  the peak.
- Profit factor: gross profit of the winning trades over gross loss of the
  losing ones; unbounded (``Decimal("Infinity")``) when some win and none
  lose, undefined (None) when none win or lose.
- Expectancy: the mean net rupee P&L per closed trade, and the mean of each
  trade's P&L as a fraction of the Equity it was sized against.

A backtest never Halts (Halt needs the Operator to clear it); ``halt_days``
lists every day the daily-loss or the drawdown Limit would have fired: a day
whose Equity is down by at least the daily-loss fraction on the day before's
(realised plus unrealised), or at least the drawdown fraction under the peak so
far. A Halt that is off (None) never fires.

A portfolio family's performance Halt is on Relative Drawdown (ADR 0020): the
fall of the Strategy's value over the Benchmark's from that ratio's running
peak, daily, before tax and with dividends counted. ``relative_drawdown_days``
lists the days it reaches the threshold; ``worst_relative_drawdown`` is how the
threshold is found from index history, then ``round_up_to_points`` rounds it
up to the next 5 points.

Values are exact Decimals; what reads them rounds them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

TRADING_DAYS_A_YEAR = 252

# The Limits that Halt (``docs/risk-controls.md``), by name.
HaltLimit = Literal["daily_loss", "drawdown"]


@dataclass(frozen=True)
class PositionResult:
    """A closed Position as the metrics read it: its net P&L after every charge and
    Slippage, and the Equity its Plan was sized against."""

    net_pnl: Decimal
    equity_at_entry: Decimal


@dataclass(frozen=True)
class Expectancy:
    rupees: Decimal
    fraction: Decimal


@dataclass(frozen=True)
class IndexHistory:
    """The stored Total Return Index history a threshold was computed from: one index's
    first and last day, its number of days, and the hash of its (day, value) rows."""

    index_name: str
    first: date
    last: date
    days: int
    hash: str


@dataclass(frozen=True)
class RelativeDrawdownHalt:
    """The Relative Drawdown Halt (ADR 0020): it fires on a day whose Relative Drawdown is
    at least ``threshold``. ``worst`` is the measure the threshold was rounded up from,
    ``source`` where it came from, ``note`` what qualifies it (the backfill before the
    index's launch, or why the fallback is not the same measure). ``history`` is the
    index history it was computed from; a ``fallback`` threshold has none."""

    threshold: Decimal
    worst: Decimal
    source: str
    fallback: bool
    note: str
    peak: date | None = None
    trough: date | None = None
    history: tuple[IndexHistory, ...] = ()


@dataclass(frozen=True)
class HaltLimits:
    """The Limits that Halt live (``docs/risk-controls.md``): a day's loss on the day
    before's Equity, and the fall from the Equity peak, as fractions, each None when the
    family turns it off; and a portfolio family's Relative Drawdown Halt, None when off."""

    daily_loss: Decimal | None
    drawdown: Decimal | None
    relative_drawdown: RelativeDrawdownHalt | None = None


@dataclass(frozen=True)
class RelativeDrawdownDay:
    """A day the Relative Drawdown Halt would have fired, with that day's Relative
    Drawdown."""

    day: date
    relative_drawdown: Decimal


@dataclass(frozen=True)
class WorstRelativeDrawdown:
    """The largest fall of a ratio from its running peak: the fall, the peak's day and the
    trough's."""

    fall: Decimal
    peak: date
    trough: date


@dataclass(frozen=True)
class HaltDay:
    """A day a Halt would have fired: the Limits that would have fired it, the day's loss on
    the day before's Equity (negative for a gain), and the drawdown from the peak so far."""

    day: date
    limits: tuple[HaltLimit, ...]
    daily_loss: Decimal
    drawdown: Decimal


def returns(curve: Sequence[Decimal]) -> list[Decimal]:
    """Each day's return on the day before's Equity."""
    return [today / before - 1 for before, today in zip(curve, curve[1:], strict=False)]


def sharpe(curve: Sequence[Decimal], *, risk_free_rate: Decimal) -> Decimal | None:
    """None when the curve has fewer than two returns or they do not vary."""
    daily = risk_free_rate / TRADING_DAYS_A_YEAR
    excess = [r - daily for r in returns(curve)]
    if len(excess) < 2:
        return None
    with localcontext() as context:
        context.prec = 34
        mean = sum(excess, Decimal(0)) / len(excess)
        variance = sum(((e - mean) ** 2 for e in excess), Decimal(0)) / (len(excess) - 1)
        if variance == 0:
            return None
        return mean / variance.sqrt() * Decimal(TRADING_DAYS_A_YEAR).sqrt()


def drawdowns(curve: Sequence[Decimal]) -> list[Decimal]:
    """Each day's fall from the peak so far, as a fraction of that peak."""
    found = []
    peak = Decimal(0)
    for equity in curve:
        peak = max(peak, equity)
        found.append((peak - equity) / peak)
    return found


def max_drawdown(curve: Sequence[Decimal]) -> Decimal:
    return max(drawdowns(curve), default=Decimal(0))


def profit_factor(pnls: Sequence[Decimal]) -> Decimal | None:
    """Infinity when some trades won and none lost; None when none won or lost."""
    profit = sum((p for p in pnls if p > 0), Decimal(0))
    loss = -sum((p for p in pnls if p < 0), Decimal(0))
    if loss == 0:
        return None if profit == 0 else Decimal("Infinity")
    return profit / loss


def expectancy(trades: Sequence[PositionResult]) -> Expectancy | None:
    """None without a closed trade."""
    if not trades:
        return None
    return Expectancy(
        sum((t.net_pnl for t in trades), Decimal(0)) / len(trades),
        sum((t.net_pnl / t.equity_at_entry for t in trades), Decimal(0)) / len(trades),
    )


def halt_days(
    days: Sequence[date],
    curve: Sequence[Decimal],
    *,
    daily_loss: Decimal | None,
    drawdown: Decimal | None,
) -> list[HaltDay]:
    """Every day either Halt Limit would have fired on, of a curve that starts at the
    starting Equity and then holds one Equity for each of ``days``."""
    if len(curve) != len(days) + 1:
        raise ValueError("the curve needs its starting Equity, then one Equity a day")
    found = []
    falls = drawdowns(curve)[1:]
    for i, (day, equity, fall) in enumerate(zip(days, curve[1:], falls, strict=True)):
        loss = (curve[i] - equity) / curve[i]
        checks: tuple[tuple[HaltLimit, Decimal, Decimal | None], ...] = (
            ("daily_loss", loss, daily_loss),
            ("drawdown", fall, drawdown),
        )
        limits = tuple(
            name for name, value, limit in checks if limit is not None and value >= limit
        )
        if limits:
            found.append(HaltDay(day, limits, loss, fall))
    return found


def relative_drawdowns(strategy: Sequence[Decimal], benchmark: Sequence[Decimal]) -> list[Decimal]:
    """Each day's Relative Drawdown: the fall of ``strategy`` over ``benchmark`` (one value
    of each a day) from that ratio's peak so far, as a fraction of the peak."""
    if len(strategy) != len(benchmark):
        raise ValueError("the Strategy and the Benchmark need one value each a day")
    return drawdowns([s / b for s, b in zip(strategy, benchmark, strict=True)])


def worst_relative_drawdown(
    days: Sequence[date], strategy: Sequence[Decimal], benchmark: Sequence[Decimal]
) -> WorstRelativeDrawdown:
    """The largest Relative Drawdown over ``days`` (the first when several are equal), with
    the day of the peak it fell from."""
    if not days or len(days) != len(strategy):
        raise ValueError("the worst Relative Drawdown needs one value of each a day")
    falls = relative_drawdowns(strategy, benchmark)
    trough = max(range(len(days)), key=lambda i: (falls[i], -i))
    ratios = [s / b for s, b in zip(strategy[: trough + 1], benchmark[: trough + 1], strict=True)]
    peak = max(range(len(ratios)), key=lambda i: (ratios[i], -i))
    return WorstRelativeDrawdown(falls[trough], days[peak], days[trough])


def round_up_to_points(fraction: Decimal, points: int = 5) -> Decimal:
    """``fraction`` rounded up to the next multiple of ``points`` percentage points (a
    multiple already is one)."""
    step = Decimal(points) / 100
    return math.ceil(fraction / step) * step


def relative_drawdown_days(
    days: Sequence[date],
    strategy: Sequence[Decimal],
    benchmark: Sequence[Decimal],
    *,
    threshold: Decimal,
) -> list[RelativeDrawdownDay]:
    """Every one of ``days`` whose Relative Drawdown is at least ``threshold``."""
    if len(days) != len(strategy):
        raise ValueError("the Relative Drawdown needs one Strategy value a day")
    return [
        RelativeDrawdownDay(day, fall)
        for day, fall in zip(days, relative_drawdowns(strategy, benchmark), strict=True)
        if fall >= threshold
    ]
