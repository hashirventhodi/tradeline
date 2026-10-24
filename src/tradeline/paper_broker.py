"""The Paper Broker's fill model: what one day's bar fills (ADR 0003 and 0004, amended for #72).

Code only, the same in every Engine Mode; in a Backtest the bar is the stored
end-of-day bar, in paper mode the next day's end-of-day bar (live quotes in
Phase 3). Every Order is a limit Order:

- An entry is an after-market limit buy at the Run day's close. It fills at the
  open when the open is at or below the limit (the pre-open auction fills a buy
  at the auction price), and is otherwise cancelled.
- A target is a limit sell at the Plan's target, resting through the day. It
  fills at the open when the open is above the target, else at the target when
  the high is strictly above it; a high equal to it fills nothing.
- A stop or time-stop exit is an after-market limit sell at the close less the
  protective band. It fills at the open when the open is at or above the limit;
  an open below leaves the Position open.
- A Circuit Lock day (open, high, low and close equal) fills nothing, and nor
  does a day the Instrument did not trade (no bar).

An exit triggers on the completed bar: a stop when the low is at or below it, a
time stop when the Position has been held its time stop in trading days. A bar
that reaches both the stop and the target is a stop: the target fills nothing
on it, whatever the order of the day's prices, so the fill model never credits
a target the day could have taken away.

A Boundary Exit closes a Position where Walk-Forward's Stitched Curve ends (the
last out-of-sample day): at the day's close, whatever the bar, or at the last
close before it when the Instrument did not trade. It is the account's close,
not an Order the market could refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tradeline.strategy.pricing import round_down

UnfilledReason = Literal[
    "no_bar", "circuit_lock", "open_above_limit", "open_below_limit", "not_reached", "stop_reached"
]
# What a completed bar can trigger: a stop, or the time stop.
ExitTrigger = Literal["stop", "time"]
# Why a Position closed: a stop or time-stop exit, its target, or a Boundary Exit.
ExitReason = Literal["stop", "time", "target", "boundary"]
# What an Order is for: an entry, or one of a Position's exits.
OrderPurpose = Literal["entry", "target", "stop", "time", "boundary"]


@dataclass(frozen=True)
class Bar:
    """One day of an Instrument as the Paper Broker sees it."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @property
    def circuit_lock(self) -> bool:
        return self.open == self.high == self.low == self.close


@dataclass(frozen=True)
class Filled:
    price: Decimal


@dataclass(frozen=True)
class Unfilled:
    reason: UnfilledReason


Outcome = Filled | Unfilled


def fill_entry(limit: Decimal, bar: Bar | None) -> Outcome:
    """An after-market limit buy at ``limit`` against the day's open."""
    if bar is None:
        return Unfilled("no_bar")
    if bar.circuit_lock:
        return Unfilled("circuit_lock")
    if bar.open > limit:
        return Unfilled("open_above_limit")
    return Filled(bar.open)


def fill_exit(limit: Decimal, bar: Bar | None) -> Outcome:
    """An after-market limit sell at ``limit`` (a stop or time-stop exit) against the open."""
    if bar is None:
        return Unfilled("no_bar")
    if bar.circuit_lock:
        return Unfilled("circuit_lock")
    if bar.open < limit:
        return Unfilled("open_below_limit")
    return Filled(bar.open)


def fill_target(target: Decimal, bar: Bar | None, *, stop: Decimal) -> Outcome:
    """A limit sell at ``target`` resting through the day, for a Position stopped at ``stop``."""
    if bar is None:
        return Unfilled("no_bar")
    if bar.circuit_lock:
        return Unfilled("circuit_lock")
    if bar.low <= stop:
        return Unfilled("stop_reached")
    if bar.open > target:
        return Filled(bar.open)
    if bar.high > target:
        return Filled(target)
    return Unfilled("not_reached")


def fill_boundary(bar: Bar | None, *, last_close: Decimal) -> Filled:
    """A Boundary Exit: at the day's close, or ``last_close`` on a day with no bar."""
    return Filled(last_close if bar is None else bar.close)


def exit_trigger(
    bar: Bar, *, stop: Decimal, days_held: int, time_stop_days: int
) -> ExitTrigger | None:
    """The exit the completed bar triggers, if any; a stop before a time stop."""
    if bar.low <= stop:
        return "stop"
    if days_held >= time_stop_days:
        return "time"
    return None


def exit_limit(close: Decimal, *, band: Decimal, tick: Decimal) -> Decimal:
    """A stop or time-stop exit's limit: the close less the band, rounded down to the tick."""
    return round_down(close * (1 - band), tick)


class FillModel:
    """The fill model as an object the engine is handed, so a backtest and a paper Run can be
    shown to fill through the very same one (ADR 0002); each method is the function above
    of the same purpose."""

    def entry(self, limit: Decimal, bar: Bar | None) -> Outcome:
        return fill_entry(limit, bar)

    def exit(self, limit: Decimal, bar: Bar | None) -> Outcome:
        return fill_exit(limit, bar)

    def target(self, target: Decimal, bar: Bar | None, *, stop: Decimal) -> Outcome:
        return fill_target(target, bar, stop=stop)

    def boundary(self, bar: Bar | None, *, last_close: Decimal) -> Filled:
        return fill_boundary(bar, last_close=last_close)

    def trigger(
        self, bar: Bar, *, stop: Decimal, days_held: int, time_stop_days: int
    ) -> ExitTrigger | None:
        return exit_trigger(bar, stop=stop, days_held=days_held, time_stop_days=time_stop_days)

    def exit_limit(self, close: Decimal, *, band: Decimal, tick: Decimal) -> Decimal:
        return exit_limit(close, band=band, tick=tick)


FILL_MODEL = FillModel()
