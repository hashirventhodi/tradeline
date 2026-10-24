"""The metrics a backtest is scored by, as the Backtest Gate will score it (ADR 0015).

Pure arithmetic over the daily marked Equity curve (each Run's Equity: cash plus
the open Positions at the day's close) and the closed trades' net P&L after
costs (``docs/risk-controls.md``, Gates):

- Sharpe: the mean of the curve's daily excess returns over the risk-free rate
  (a yearly rate taken as rate / 252 a trading day; idle cash earns nothing),
  over their sample standard deviation, times the square root of 252.
- Max drawdown: the largest peak-to-trough fall of the curve, as a fraction of
  the peak.
- Profit factor: gross profit of the winning trades over gross loss of the
  losing ones.
- Expectancy: the mean net rupee P&L per closed trade, and the mean of each
  trade's P&L as a fraction of the Equity it was sized against.

A backtest never Halts (Halt needs the Operator to clear it); ``halt_days``
lists every day the daily-loss or the drawdown Limit would have fired: a day
whose Equity is down by at least the daily-loss fraction on the day before's
(realised plus unrealised), or at least the drawdown fraction under the peak so
far.

Values are exact Decimals; what reads them rounds them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

TRADING_DAYS_A_YEAR = 252


@dataclass(frozen=True)
class Trade:
    """A closed trade as the metrics read it: its net P&L after every charge and Slippage,
    and the Equity its Plan was sized against."""

    net_pnl: Decimal
    equity_at_entry: Decimal


@dataclass(frozen=True)
class Expectancy:
    rupees: Decimal
    fraction: Decimal


@dataclass(frozen=True)
class HaltLimits:
    """The Limits that Halt live (``docs/risk-controls.md``): a day's loss on the day
    before's Equity, and the fall from the Equity peak, as fractions."""

    daily_loss: Decimal = Decimal("0.03")
    drawdown: Decimal = Decimal("0.10")


@dataclass(frozen=True)
class HaltDay:
    """A day a Halt would have fired: the Limits that would have fired it, the day's loss on
    the day before's Equity (negative for a gain), and the drawdown from the peak so far."""

    day: date
    limits: tuple[str, ...]
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
    """None when no trade lost."""
    profit = sum((p for p in pnls if p > 0), Decimal(0))
    loss = -sum((p for p in pnls if p < 0), Decimal(0))
    return None if loss == 0 else profit / loss


def expectancy(trades: Sequence[Trade]) -> Expectancy | None:
    """None without a closed trade."""
    if not trades:
        return None
    return Expectancy(
        sum((t.net_pnl for t in trades), Decimal(0)) / len(trades),
        sum((t.net_pnl / t.equity_at_entry for t in trades), Decimal(0)) / len(trades),
    )


def halt_days(
    days: Sequence[date], curve: Sequence[Decimal], *, daily_loss: Decimal, drawdown: Decimal
) -> list[HaltDay]:
    """Every day of the curve either Halt Limit would have fired on."""
    if len(days) != len(curve):
        raise ValueError("the curve needs one Equity a day")
    found = []
    falls = drawdowns(curve)
    for i, (day, equity, fall) in enumerate(zip(days, curve, falls, strict=True)):
        loss = Decimal(0) if i == 0 else (curve[i - 1] - equity) / curve[i - 1]
        checks = (("daily_loss", loss, daily_loss), ("drawdown", fall, drawdown))
        limits = tuple(name for name, value, limit in checks if value >= limit)
        if limits:
            found.append(HaltDay(day, limits, loss, fall))
    return found
