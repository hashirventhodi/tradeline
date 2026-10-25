"""The Risk Engine: every Plan of a Run checked against the Limits, ending in a Pass or a Block.

Code only, and the same in every Engine Mode (ADR 0002). Plans are checked in
Plan order as if each Pass before had filled, so Plans that are fine alone but
breach a Limit together are caught, and the first to Pass are the same on
every run (``docs/risk-controls.md``).

Every Limit is declared, on with its value or off, in the family's Protocol
(ADR 0020); a Limit set that leaves one unstated is refused before any Plan is
checked, so a Limit added later can never default to off, or on, for a family
that never stated it. A Limit that is off is skipped. Risk per trade and the
minimum reward to risk are read off the stop, so they can be on only while a
stop is required.

A Plan with a stop (``tradeline.strategy.Plan``) is first priced
(``tradeline.strategy.pricing``): stop rounded down to the tick in force,
target at the Plan's multiple of the rounded risk rounded up, sized at
floor(Equity x risk per trade / (entry - stop)) shares, which needs risk per
trade on (it is refused with risk per trade off). A portfolio Strategy's Plan
(``PortfolioPlan``) has an entry price and a size only, the size its
caller's. Then, in this order, a Plan is Blocked for:

- ``stop_required``, when on: no stop under the entry once rounded (and above
  zero), which a ``PortfolioPlan`` never has;
- ``min_reward_to_risk``, when on: (target - entry) / (entry - stop) under the
  minimum, on the rounded prices;
- ``max_new_entries``, when on: the Run has already Passed the most new entries;
- ``max_open_positions``, when on: open Positions plus this Run's Passes are already the most;

and its size is reduced to what each of ``max_position`` (notional at entry),
``max_sector`` (the sector's held and Passed notional), ``max_gross`` (all
held and Passed notional) and ``participation_cap`` (a fraction of the
20-day average daily traded value) that is on allows. An Order then of no
shares, or worth less than the minimum Order value, is Blocked, naming the
Limit that reduced it most, or ``min_order_value`` when none reduced it; with
the minimum Order value off, an Order of no shares is still Blocked so. A Block
carries the Plan priced and sized as far as it got.

A Plan whose Instrument has no recorded sector is in ``UNCLASSIFIED``, which
the sector Limit applies to like any other.

A Pass is bound to its Plan's content hash and its Run's id: ``verify_pass``
rejects it for any other Plan or Run, so a Plan changed after its Pass, in any
field, carries none.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import astuple, dataclass, field, fields
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


class Unstated(enum.Enum):
    """A Limit a Limit set does not state, on or off."""

    UNSTATED = "unstated"


UNSTATED = Unstated.UNSTATED


class UnstatedLimits(ValueError):
    """A Limit set that leaves a Limit unstated (ADR 0020)."""


@dataclass(frozen=True, kw_only=True)
class Limits:
    """The Limits the Risk Engine enforces at a Run, each on with its value or off (None);
    fractions of Equity where they are. A Limit not given is unstated, which the Risk Engine
    and a Protocol refuse (``unstated``)."""

    # A Plan without a stop under its entry is Blocked.
    stop_required: bool | Unstated = UNSTATED
    risk_per_trade: Decimal | None | Unstated = UNSTATED
    max_open_positions: int | None | Unstated = UNSTATED
    max_position: Decimal | None | Unstated = UNSTATED
    max_sector: Decimal | None | Unstated = UNSTATED
    max_gross: Decimal | None | Unstated = UNSTATED
    # Of the 20-day average daily traded value.
    participation_cap: Decimal | None | Unstated = UNSTATED
    min_order_value: Decimal | None | Unstated = UNSTATED
    max_new_entries: int | None | Unstated = UNSTATED
    min_reward_to_risk: Decimal | None | Unstated = UNSTATED

    def __post_init__(self) -> None:
        if self.stop_required is False:
            for name in ("risk_per_trade", "min_reward_to_risk"):
                if getattr(self, name) not in (None, UNSTATED):
                    raise ValueError(
                        f"{name} is read off the stop, so it cannot be on while stop_required"
                        " is off"
                    )

    def unstated(self) -> tuple[str, ...]:
        """The Limits this set states neither on nor off, in declaration order."""
        return tuple(f.name for f in fields(self) if getattr(self, f.name) is UNSTATED)

    def require_stated(self) -> None:
        """Refuse, naming each, if any Limit is unstated."""
        missing = self.unstated()
        if missing:
            raise UnstatedLimits(
                f"the Limits leave {', '.join(missing)} unstated; every Limit is stated on,"
                " with its value, or off (ADR 0020)"
            )


@dataclass(frozen=True)
class _Stated:
    """A Limit set with every Limit stated: what ``assess`` reads."""

    stop_required: bool
    risk_per_trade: Decimal | None
    max_open_positions: int | None
    max_position: Decimal | None
    max_sector: Decimal | None
    max_gross: Decimal | None
    participation_cap: Decimal | None
    min_order_value: Decimal | None
    max_new_entries: int | None
    min_reward_to_risk: Decimal | None


def _stated(limits: Limits) -> _Stated:
    limits.require_stated()
    return _Stated(**{f.name: getattr(limits, f.name) for f in fields(limits)})


@dataclass(frozen=True)
class PortfolioPlan:
    """A portfolio Strategy's Plan: an entry price and a size only, the size its caller's,
    held until it leaves at a Rebalance. It has no stop, target or time stop."""

    rank: int
    instrument: str
    symbol: str
    entry: Decimal
    shares: int

    @property
    def stop(self) -> None:
        return None

    @property
    def target(self) -> None:
        return None

    @property
    def time_stop_days(self) -> None:
        return None

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


# A Plan as the Risk Engine decides on it: priced on the tick with a size, or a portfolio
# Plan with its size.
DecidedPlan = PricedPlan | PortfolioPlan


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

    plan: Plan | PortfolioPlan
    # The tick in force for it on the Run's day (``tradeline.tick_sizes``).
    tick: Decimal
    # Its sector as of the Run; None when it has none recorded.
    sector: str | None
    # Its 20-day average daily traded value, in rupees.
    average_traded_value: Decimal


@dataclass(frozen=True)
class Pass:
    run_id: uuid.UUID
    plan: DecidedPlan
    plan_hash: str
    sector: str
    # The shares each sizing Limit allowed, the size it started from first: ``risk_per_trade``
    # for a Plan sized by its risk, ``requested`` for a portfolio Plan.
    allowed: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Block:
    run_id: uuid.UUID
    plan: DecidedPlan
    limit: Limit
    sector: str
    allowed: Mapping[str, int] = field(default_factory=dict)


Decision = Pass | Block


class PassRejected(ValueError):
    """A Pass offered for a Plan or a Run other than the one it was issued for."""


def verify_pass(granted: Pass, plan: DecidedPlan, *, run_id: uuid.UUID) -> None:
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
    stated = _stated(limits)
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
        sector = _sector(proposal.sector)
        if stated.stop_required and isinstance(proposal.plan, PortfolioPlan):
            decisions.append(Block(run_id, proposal.plan, "stop_required", sector))
            continue
        priced, start = _priced(proposal, equity=equity, limits=stated)
        entry = priced.entry
        if stated.stop_required and not (priced.stop is not None and 0 < priced.stop < entry):
            decisions.append(Block(run_id, priced, "stop_required", sector))
            continue
        allowed: dict[str, int] = {start: priced.shares}
        if stated.max_position is not None:
            allowed["max_position"] = shares_within(equity * stated.max_position, entry)
        if stated.max_sector is not None:
            allowed["max_sector"] = shares_within(
                equity * stated.max_sector - by_sector.get(sector, Decimal(0)), entry
            )
        if stated.max_gross is not None:
            allowed["max_gross"] = shares_within(equity * stated.max_gross - gross, entry)
        if stated.participation_cap is not None:
            allowed["participation_cap"] = shares_within(
                proposal.average_traded_value * stated.participation_cap, entry
            )
        # The first of the smallest names the Limit that bound.
        binding = min(allowed, key=lambda name: allowed[name])
        sized = dataclasses.replace(priced, shares=allowed[binding])
        limit: Limit | None = None
        if stated.min_reward_to_risk is not None and _reward_to_risk(sized) < (
            stated.min_reward_to_risk
        ):
            limit = "min_reward_to_risk"
        elif stated.max_new_entries is not None and passes >= stated.max_new_entries:
            limit = "max_new_entries"
        elif stated.max_open_positions is not None and open_positions >= stated.max_open_positions:
            limit = "max_open_positions"
        elif sized.shares == 0 or (
            stated.min_order_value is not None and sized.order_value < stated.min_order_value
        ):
            limit = "min_order_value" if binding == start else _as_limit(binding)
        if limit is not None:
            decisions.append(Block(run_id, sized, limit, sector, allowed))
            continue
        decisions.append(Pass(run_id, sized, sized.content_hash(), sector, allowed))
        passes += 1
        open_positions += 1
        gross += sized.order_value
        by_sector[sector] = by_sector.get(sector, Decimal(0)) + sized.order_value
    return decisions


def _priced(proposal: Proposal, *, equity: Decimal, limits: _Stated) -> tuple[DecidedPlan, str]:
    """The Plan as sized before any Limit reduces it, and what sized it. A portfolio Plan
    reaches here only with a stop not required, so with risk per trade off."""
    plan = proposal.plan
    if isinstance(plan, PortfolioPlan):
        return plan, "requested"
    if limits.risk_per_trade is None:
        raise ValueError(f"{plan.symbol}'s Plan is sized by its risk, and risk per trade is off")
    priced = price_plan(
        plan, tick=proposal.tick, equity=equity, risk_per_trade=limits.risk_per_trade
    )
    return priced, "risk_per_trade"


def _reward_to_risk(plan: DecidedPlan) -> Decimal:
    # Read only with a stop required, which a portfolio Plan never passes.
    assert isinstance(plan, PricedPlan)
    return plan.reward_to_risk


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
