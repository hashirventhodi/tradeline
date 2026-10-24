"""Plan pricing: a Strategy's Plan put on the tick, with its size at the risk per trade.

A Strategy prices its Plan from indicators, so its stop and target can fall
between ticks. Pricing rounds the stop and the target down to the tick in
force that day (``tradeline.tick_sizes``) and sizes the Plan at
floor(Equity x risk per trade / (entry - stop)) shares, so rounding only ever
widens the risk and narrows the reward. The entry, a close, is a traded price
and already on the tick. The minimum reward to risk is the Risk Engine's to
re-check on the rounded prices (``PricedPlan.reward_to_risk``), as are the
Limits that reduce the size.

Every Strategy's Plans go through this module, so it is part of every
Strategy's code revision (``tradeline.revision``).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import astuple, dataclass, fields
from decimal import ROUND_FLOOR, Decimal
from fractions import Fraction

from tradeline.strategy import Plan


@dataclass(frozen=True)
class PricedPlan:
    """A Plan on the tick, with a size in whole shares."""

    rank: int
    instrument: str
    symbol: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    time_stop_days: int
    shares: int

    @property
    def reward_to_risk(self) -> Decimal:
        return (self.target - self.entry) / (self.entry - self.stop)

    @property
    def order_value(self) -> Decimal:
        return self.entry * self.shares

    def content_hash(self) -> str:
        """SHA-256 of every field; a Decimal by its value, so 274.0 and 274.00 hash alike."""
        values = {
            f.name: f"{v.normalize():f}" if isinstance(v, Decimal) else v
            for f, v in zip(fields(self), astuple(self), strict=True)
        }
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def round_down(price: Decimal | Fraction, tick: Decimal) -> Decimal:
    """``price`` rounded down to the tick, exactly."""
    return math.floor(Fraction(price) / Fraction(tick)) * tick


def shares_within(budget: Decimal, per_share: Decimal) -> int:
    """The whole shares ``budget`` buys at ``per_share`` each; none for a budget under zero."""
    if budget <= 0:
        return 0
    return int((budget / per_share).to_integral_value(rounding=ROUND_FLOOR))


def price_plan(
    plan: Plan, *, tick: Decimal, equity: Decimal, risk_per_trade: Decimal
) -> PricedPlan:
    """``plan`` with its stop and target rounded down to ``tick`` and sized at the risk per
    trade. A stop that does not round to a price under the entry sizes the Plan at no shares."""
    stop = round_down(plan.stop, tick)
    target = round_down(plan.target, tick)
    risk = plan.entry - stop
    shares = shares_within(equity * risk_per_trade, risk) if stop > 0 and risk > 0 else 0
    return PricedPlan(
        rank=plan.rank,
        instrument=plan.instrument,
        symbol=plan.symbol,
        entry=plan.entry,
        stop=stop,
        target=target,
        time_stop_days=plan.time_stop_days,
        shares=shares,
    )
