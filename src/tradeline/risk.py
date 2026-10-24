"""The Risk Engine: every Plan of a Run checked against the Limits, ending in a Pass or a Block.

Code only, and the same in every Engine Mode (ADR 0002). Plans are checked in
Plan order as if each Pass before had filled, so Plans that are fine alone but
breach a Limit together are caught, and the first to Pass are the same on
every run (``docs/risk-controls.md``).

Each Plan is first priced (``tradeline.strategy.pricing``): stop and target
rounded down to the tick in force, sized at floor(Equity x risk per trade /
(entry - stop)) shares. Then, in this order, it is Blocked for:

- ``stop_required``: no stop under the entry once rounded (and above zero);
- ``min_reward_to_risk``: (target - entry) / (entry - stop) under the minimum,
  on the rounded prices;
- ``max_new_entries``: the Run has already Passed the most new entries;
- ``max_open_positions``: open Positions plus this Run's Passes are already the most;

and its size is reduced to what each of ``max_position`` (notional at entry),
``max_sector`` (the sector's held and Passed notional), ``max_gross`` (all
held and Passed notional) and ``participation_cap`` (a fraction of the
20-day average daily traded value) allows. An Order then of no shares, or
worth less than the minimum Order value, is Blocked, naming the Limit that reduced it most, or
``min_order_value`` when none reduced it. A Block carries the Plan priced and
sized as far as it got.

A Plan whose Instrument has no recorded sector is in ``UNCLASSIFIED``, which
the sector Limit applies to like any other.

A Pass is bound to its Plan's content hash and its Run's id: ``verify_pass``
rejects it for any other Plan or Run, so a Plan changed after its Pass, in any
field, carries none.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from tradeline.strategy import Plan
from tradeline.strategy.pricing import PricedPlan, price_plan, shares_within

UNCLASSIFIED = "unclassified"

Limit = Literal[
    "stop_required",
    "min_reward_to_risk",
    "max_new_entries",
    "max_open_positions",
    "max_position",
    "max_sector",
    "max_gross",
    "participation_cap",
    "min_order_value",
]


@dataclass(frozen=True)
class Limits:
    """The Limits the Risk Engine enforces at a Run, as fractions of Equity where they are."""

    risk_per_trade: Decimal = Decimal("0.01")
    max_open_positions: int = 8
    max_position: Decimal = Decimal("0.15")
    max_sector: Decimal = Decimal("0.30")
    max_gross: Decimal = Decimal("1.00")
    # Of the 20-day average daily traded value.
    participation_cap: Decimal = Decimal("0.01")
    min_order_value: Decimal = Decimal("5000")
    max_new_entries: int = 4
    min_reward_to_risk: Decimal = Decimal("1.5")


@dataclass(frozen=True)
class Holding:
    """An open Position as the Limits see it: its sector (None: Unclassified) and its value
    at the day's close."""

    instrument: str
    sector: str | None
    value: Decimal


@dataclass(frozen=True)
class Portfolio:
    equity: Decimal
    holdings: tuple[Holding, ...] = ()


@dataclass(frozen=True)
class Proposal:
    """A Plan put to the Risk Engine, with what the Limits read about its Instrument."""

    plan: Plan
    # The tick in force for it on the Run's day (``tradeline.tick_sizes``).
    tick: Decimal
    # Its sector as of the Run; None when it has none recorded.
    sector: str | None
    # Its 20-day average daily traded value, in rupees.
    average_traded_value: Decimal


@dataclass(frozen=True)
class Pass:
    run_id: uuid.UUID
    plan: PricedPlan
    plan_hash: str
    sector: str
    # The shares each sizing Limit allowed, ``risk_per_trade`` first.
    allowed: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Block:
    run_id: uuid.UUID
    plan: PricedPlan
    limit: Limit
    sector: str
    allowed: Mapping[str, int] = field(default_factory=dict)


Decision = Pass | Block


class PassRejected(ValueError):
    """A Pass offered for a Plan or a Run other than the one it was issued for."""


def verify_pass(granted: Pass, plan: PricedPlan, *, run_id: uuid.UUID) -> None:
    """Refuse ``granted`` unless it was issued for exactly ``plan`` in the Run ``run_id``."""
    if granted.run_id != run_id:
        raise PassRejected(f"the Pass was issued in Run {granted.run_id}, not {run_id}")
    if granted.plan_hash != plan.content_hash():
        raise PassRejected(
            f"the Pass for {granted.plan.symbol} (rank {granted.plan.rank}) was issued for"
            " another Plan"
        )


def assess(
    proposals: Sequence[Proposal],
    *,
    portfolio: Portfolio,
    limits: Limits,
    run_id: uuid.UUID,
) -> list[Decision]:
    """A Pass or a Block for each Proposal, in the order given (the Plan order)."""
    equity = portfolio.equity
    open_positions = len(portfolio.holdings)
    gross = sum((h.value for h in portfolio.holdings), Decimal(0))
    by_sector: dict[str, Decimal] = {}
    for h in portfolio.holdings:
        name = _sector(h.sector)
        by_sector[name] = by_sector.get(name, Decimal(0)) + h.value
    passes = 0
    decisions: list[Decision] = []
    for proposal in proposals:
        priced = price_plan(
            proposal.plan,
            tick=proposal.tick,
            equity=equity,
            risk_per_trade=limits.risk_per_trade,
        )
        sector = _sector(proposal.sector)
        entry = priced.entry
        if not 0 < priced.stop < entry:
            decisions.append(Block(run_id, priced, "stop_required", sector))
            continue
        allowed: dict[str, int] = {
            "risk_per_trade": priced.shares,
            "max_position": shares_within(equity * limits.max_position, entry),
            "max_sector": shares_within(
                equity * limits.max_sector - by_sector.get(sector, Decimal(0)), entry
            ),
            "max_gross": shares_within(equity * limits.max_gross - gross, entry),
            "participation_cap": shares_within(
                proposal.average_traded_value * limits.participation_cap, entry
            ),
        }
        # The first of the smallest names the Limit that bound.
        binding = min(allowed, key=lambda name: allowed[name])
        sized = PricedPlan(
            rank=priced.rank,
            instrument=priced.instrument,
            symbol=priced.symbol,
            entry=entry,
            stop=priced.stop,
            target=priced.target,
            time_stop_days=priced.time_stop_days,
            shares=allowed[binding],
        )
        limit: Limit | None = None
        if sized.reward_to_risk < limits.min_reward_to_risk:
            limit = "min_reward_to_risk"
        elif passes >= limits.max_new_entries:
            limit = "max_new_entries"
        elif open_positions >= limits.max_open_positions:
            limit = "max_open_positions"
        elif sized.shares == 0 or sized.order_value < limits.min_order_value:
            limit = "min_order_value" if binding == "risk_per_trade" else _as_limit(binding)
        if limit is not None:
            decisions.append(Block(run_id, sized, limit, sector, allowed))
            continue
        decisions.append(Pass(run_id, sized, sized.content_hash(), sector, allowed))
        passes += 1
        open_positions += 1
        gross += sized.order_value
        by_sector[sector] = by_sector.get(sector, Decimal(0)) + sized.order_value
    return decisions


class RiskEngine:
    """The Risk Engine as an object the engine is handed, so a backtest and a paper Run can
    be shown to use the very same one (ADR 0002); ``assess`` is all it does."""

    def assess(
        self,
        proposals: Sequence[Proposal],
        *,
        portfolio: Portfolio,
        limits: Limits,
        run_id: uuid.UUID,
    ) -> list[Decision]:
        return assess(proposals, portfolio=portfolio, limits=limits, run_id=run_id)


RISK_ENGINE = RiskEngine()


def _sector(name: str | None) -> str:
    return UNCLASSIFIED if name is None else name


def _as_limit(name: str) -> Limit:
    limits: dict[str, Limit] = {
        "max_position": "max_position",
        "max_sector": "max_sector",
        "max_gross": "max_gross",
        "participation_cap": "participation_cap",
    }
    return limits[name]
