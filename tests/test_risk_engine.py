"""The Risk Engine over hand-built portfolios and Plans, with no database.

Unless a test says otherwise the Limits are ``docs/risk-controls.md``'s
defaults (``pullback_v1``'s), Equity is Rs 5,00,000 and the tick is 5 paisa: 1 percent of Equity
is Rs 5,000 at risk, 15 percent is Rs 75,000 per Position, 30 percent is
Rs 1,50,000 per sector. Every expected Pass and Block is worked by hand from
those numbers.
"""

import uuid
from dataclasses import fields, replace
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from limits import LOWVOL, PULLBACK
from tradeline.risk import (
    UNCLASSIFIED,
    Block,
    Holding,
    Limits,
    Pass,
    PassRejected,
    Portfolio,
    PortfolioPlan,
    Proposal,
    UnstatedLimits,
    assess,
    verify_pass,
)
from tradeline.strategy import Plan
from tradeline.strategy.pricing import PricedPlan

D = Decimal
EQUITY = D("500000")
LIMITS = PULLBACK
TICK = D("0.05")
LIQUID = D("1000000000")  # a 20-day average traded value no Order comes near 1 percent of
RUN = uuid.uuid4()


def proposal(
    rank: int,
    *,
    entry: str = "100",
    stop: str = "95",
    target: str = "110",
    sector: str | None = "Capital Goods",
    traded_value: Decimal = LIQUID,
) -> Proposal:
    plan = Plan(rank, f"S{rank}", f"S{rank}", D(entry), D(stop), D(target), 10, {})
    return Proposal(plan, tick=TICK, sector=sector, average_traded_value=traded_value)


def decide(proposals: list[Proposal], *holdings: Holding) -> list[Pass | Block]:
    return assess(proposals, portfolio=Portfolio(EQUITY, holdings), limits=LIMITS, run_id=RUN)


def outcome(decisions: list[Pass | Block]) -> list[tuple[int, str, int]]:
    """(rank, the Limit that Blocked it or "pass", shares) for each decision."""
    return [
        (d.plan.rank, d.limit if isinstance(d, Block) else "pass", d.plan.shares) for d in decisions
    ]


def test_a_plan_is_sized_at_the_risk_per_trade_then_reduced_to_the_per_position_limit() -> None:
    # 5,000 at risk over 5 a share is 1,000 shares, Rs 1,00,000: over 75,000, so 750.
    (decision,) = decide([proposal(1)])

    assert isinstance(decision, Pass)
    assert decision.plan.shares == 750
    assert decision.run_id == RUN


def test_five_plans_fine_alone_but_over_gross_exposure_together_block_from_the_third() -> None:
    # Four Positions hold 3,50,000 of the 5,00,000, leaving 1,50,000 of gross
    # exposure: two Plans of 75,000 each use it all.
    holdings = [Holding(f"H{i}", f"Sector {i}", D("87500")) for i in range(4)]
    plans = [proposal(rank, sector=f"Plan sector {rank}") for rank in range(1, 6)]

    for plan in plans:
        assert outcome(decide([plan], *holdings)) == [(plan.plan.rank, "pass", 750)]
    assert outcome(decide(plans, *holdings)) == [
        (1, "pass", 750),
        (2, "pass", 750),
        (3, "max_gross", 0),
        (4, "max_gross", 0),
        (5, "max_gross", 0),
    ]


def test_a_candidate_beyond_the_fourth_entry_of_a_run_is_blocked() -> None:
    # Five Plans of 75,000 in five sectors: 3,75,000, inside gross exposure.
    plans = [proposal(rank, sector=f"Sector {rank}") for rank in range(1, 6)]

    assert [d.limit for d in decide(plans) if isinstance(d, Block)] == ["max_new_entries"]
    assert outcome(decide(plans))[-1] == (5, "max_new_entries", 750)


def test_a_plan_is_blocked_when_the_portfolio_holds_the_most_open_positions() -> None:
    holdings = [Holding(f"H{i}", f"Sector {i}", D("10000")) for i in range(8)]

    assert outcome(decide([proposal(1)], *holdings)) == [(1, "max_open_positions", 750)]


def test_an_order_over_the_participation_cap_is_reduced_and_blocked_under_the_minimum() -> None:
    # 1 percent of 4,00,000 a day is 4,000: 40 shares, under the 5,000 minimum.
    blocked = proposal(1, traded_value=D("400000"))
    # 1 percent of 20,00,000 is 20,000: 200 shares, an Order of 20,000.
    reduced = proposal(1, traded_value=D("2000000"))

    assert outcome(decide([blocked])) == [(1, "participation_cap", 40)]
    assert outcome(decide([reduced])) == [(1, "pass", 200)]


def test_an_unclassified_instrument_counts_toward_the_unclassified_sectors_30_percent() -> None:
    # 1,40,000 already Unclassified leaves 10,000 of the 1,50,000: 100 shares.
    held = Holding("H", None, D("140000"))
    # A second Unclassified Plan finds 0 left.
    plans = [proposal(1, sector=None), proposal(2, sector=None)]

    decisions = decide(plans, held)

    assert outcome(decisions) == [(1, "pass", 100), (2, "max_sector", 0)]
    assert [d.sector for d in decisions] == [UNCLASSIFIED, UNCLASSIFIED]


def test_a_plan_whose_reward_to_risk_is_under_the_minimum_is_blocked() -> None:
    # 10.00 / 6.70, both prices on the tick.
    plan = proposal(1, entry="200", stop="193.30", target="210.00")

    (decision,) = decide([plan])

    assert isinstance(decision, Block)
    assert decision.limit == "min_reward_to_risk"
    assert (decision.plan.stop, decision.plan.target) == (D("193.30"), D("210.00"))


def test_rounding_does_not_take_a_plan_at_the_minimum_under_it() -> None:
    # 10.04 / 6.69 before rounding. Rounding both prices down, as pricing did until #85,
    # gave 10.00 / 6.70, under 1.5; the target now keeps the multiple over 6.70, rounded up.
    plan = proposal(1, entry="200", stop="193.31", target="210.04")

    (decision,) = decide([plan])

    assert not isinstance(decision, Block)
    assert (decision.plan.stop, decision.plan.target) == (D("193.30"), D("210.10"))


@pytest.mark.parametrize("stop", ["100", "101", "0.04"], ids=["at-entry", "above", "rounds-to-0"])
def test_a_plan_without_a_stop_below_its_entry_is_blocked(stop: str) -> None:
    assert outcome(decide([proposal(1, stop=stop)])) == [(1, "stop_required", 0)]


def test_a_plan_sized_under_the_minimum_order_value_by_its_risk_alone_names_that_limit() -> None:
    # Rs 50,000 of Equity puts 500 at risk: over 99.95 a share, 5 shares of Rs 100,
    # an Order of 500, under the 5,000 minimum, with no other Limit reducing it.
    plan = proposal(1, entry="100", stop="0.05", target="300")
    small = assess([plan], portfolio=Portfolio(D("50000")), limits=LIMITS, run_id=RUN)

    assert outcome(small) == [(1, "min_order_value", 5)]


# --- Limits declared on or off (ADR 0020) --------------------------------------


def test_a_limit_set_that_leaves_a_limit_unstated_is_refused_naming_it() -> None:
    stated = {f.name: getattr(PULLBACK, f.name) for f in fields(Limits) if f.name != "max_sector"}

    with pytest.raises(UnstatedLimits, match="max_sector"):
        assess([proposal(1)], portfolio=Portfolio(EQUITY), limits=Limits(**stated), run_id=RUN)


@pytest.mark.parametrize("limit", ["risk_per_trade", "min_reward_to_risk"])
def test_a_limit_read_off_the_stop_cannot_be_on_while_a_stop_is_not_required(limit: str) -> None:
    with pytest.raises(ValueError, match=limit):
        replace(LOWVOL, **{limit: getattr(PULLBACK, limit)})


def unstopped(
    rank: int,
    shares: int,
    *,
    entry: str = "500",
    sector: str | None = "Capital Goods",
    traded_value: Decimal = LIQUID,
) -> Proposal:
    """A portfolio Plan: an entry and its caller's size, no stop, no target, no time stop."""
    plan = PortfolioPlan(rank, f"S{rank}", f"S{rank}", D(entry), shares)
    return Proposal(plan, tick=TICK, sector=sector, average_traded_value=traded_value)


def decide_lowvol(proposals: list[Proposal], *holdings: Holding) -> list[Pass | Block]:
    return assess(proposals, portfolio=Portfolio(EQUITY, holdings), limits=LOWVOL, run_id=RUN)


def test_with_the_stop_limits_off_a_plan_with_no_stop_passes_at_the_callers_size() -> None:
    # 100 shares at 500 is 50,000, 10 percent of Equity.
    (decision,) = decide_lowvol([unstopped(1, 100)])

    assert isinstance(decision, Pass)
    assert (decision.plan.shares, decision.plan.stop, decision.plan.target) == (100, None, None)
    # 15 percent is 150 shares, all of Equity 1,000, 1 percent of the traded value 20,000.
    assert decision.allowed == {
        "requested": 100,
        "max_position": 150,
        "max_gross": 1000,
        "participation_cap": 20000,
    }


def test_with_the_stop_limits_off_the_per_position_limit_still_reduces_and_blocks() -> None:
    # 15 percent of 5,00,000 is 75,000: 150 shares at 500; none at 80,000 a share.
    assert outcome(decide_lowvol([unstopped(1, 200)])) == [(1, "pass", 150)]
    assert outcome(decide_lowvol([unstopped(1, 1, entry="80000")])) == [(1, "max_position", 0)]


def test_with_the_stop_limits_off_gross_exposure_still_blocks() -> None:
    # 4,98,000 held leaves 2,000: 4 shares at 500, an Order under 5,000.
    held = [Holding(f"H{i}", f"Sector {i}", D("49800")) for i in range(10)]

    assert outcome(decide_lowvol([unstopped(1, 100)], *held)) == [(1, "max_gross", 4)]


def test_with_the_stop_limits_off_the_participation_cap_still_blocks() -> None:
    # 1 percent of 4,00,000 is 4,000: 8 shares at 500, under the 5,000 minimum.
    blocked = unstopped(1, 100, traded_value=D("400000"))

    assert outcome(decide_lowvol([blocked])) == [(1, "participation_cap", 8)]


def test_with_the_stop_limits_off_an_order_under_the_minimum_order_value_is_blocked() -> None:
    # 9 shares at 500 is 4,500, and no Limit reduced it.
    assert outcome(decide_lowvol([unstopped(1, 9)])) == [(1, "min_order_value", 9)]


def test_the_lowvol_set_holds_at_most_thirty_positions() -> None:
    held = [Holding(f"H{i}", f"Sector {i}", D("10000")) for i in range(29)]

    assert outcome(decide_lowvol([unstopped(1, 20), unstopped(2, 20)], *held)) == [
        (1, "pass", 20),
        (2, "max_open_positions", 20),
    ]


def test_with_the_sector_and_new_entries_limits_off_neither_reduces_nor_blocks() -> None:
    # Twelve Plans of 20,000, all Unclassified: 2,40,000, over 30 percent and 4 entries.
    plans = [unstopped(rank, 40, sector=None) for rank in range(1, 13)]

    assert outcome(decide_lowvol(plans)) == [(rank, "pass", 40) for rank in range(1, 13)]


def test_with_a_stop_required_a_portfolio_plan_is_blocked() -> None:
    assert outcome(decide([unstopped(1, 100)])) == [(1, "stop_required", 100)]


def test_a_plan_sized_by_its_risk_is_refused_with_risk_per_trade_off() -> None:
    with pytest.raises(ValueError, match="risk per trade is off"):
        decide_lowvol([proposal(1)])


# --- A Pass is bound to its Plan and its Run -----------------------------------


def a_pass() -> Pass:
    (decision,) = decide([proposal(1)])
    assert isinstance(decision, Pass)
    return decision


def changed(value: object) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, Decimal):
        return value + D("0.05")
    assert isinstance(value, str)
    return value + "X"


def test_a_pass_is_accepted_for_its_own_plan_and_run() -> None:
    granted = a_pass()

    verify_pass(granted, granted.plan, run_id=RUN)


@pytest.mark.parametrize("field", [f.name for f in fields(PricedPlan)])
def test_a_pass_is_rejected_when_any_field_of_its_plan_changes(field: str) -> None:
    granted = a_pass()
    edited = replace(granted.plan, **{field: changed(getattr(granted.plan, field))})

    with pytest.raises(PassRejected):
        verify_pass(granted, edited, run_id=RUN)


def test_a_pass_is_rejected_for_another_run() -> None:
    granted = a_pass()

    with pytest.raises(PassRejected):
        verify_pass(granted, granted.plan, run_id=uuid.uuid4())


# --- No Pass breaches a Limit -------------------------------------------------

SECTORS = st.sampled_from([None, "Capital Goods", "Chemicals", "Financial Services"])
RUPEES = st.decimals(min_value=D("1"), max_value=D("5000"), places=2)


@st.composite
def holdings(draw: st.DrawFn) -> Holding:
    value = draw(st.decimals(min_value=D("0"), max_value=D("200000"), places=2))
    return Holding(draw(st.text("ABCDEF", min_size=1, max_size=3)), draw(SECTORS), value)


@st.composite
def proposals(draw: st.DrawFn, rank: int, limits: Limits) -> Proposal:
    """A Plan with a stop and a target while risk per trade is on, and otherwise a portfolio
    Plan with its caller's size."""
    entry = draw(RUPEES)
    plan: Plan | PortfolioPlan
    if limits.risk_per_trade is None:
        plan = PortfolioPlan(rank, f"P{rank}", f"P{rank}", entry, draw(st.integers(0, 5000)))
    else:
        stop = entry - draw(st.decimals(min_value=D("-1"), max_value=entry, places=3))
        target = entry + draw(st.decimals(min_value=D("0"), max_value=entry, places=3))
        plan = Plan(rank, f"P{rank}", f"P{rank}", entry, stop, target, 10, {})
    tick = draw(st.sampled_from([D("0.01"), D("0.05"), D("0.10")]))
    traded_value = draw(st.decimals(min_value=D("0"), max_value=D("1e9"), places=0))
    return Proposal(plan, tick=tick, sector=draw(SECTORS), average_traded_value=traded_value)


def fraction(low: str, high: str) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=D(low), max_value=D(high), places=3)


def on_or_off[T](value: st.SearchStrategy[T]) -> st.SearchStrategy[T | None]:
    return st.one_of(st.none(), value)


@st.composite
def generated_limits(draw: st.DrawFn) -> Limits:
    """Every Limit on, with a value, or off; those read off the stop on only when a stop is
    required."""
    stop_required = draw(st.booleans())
    off: st.SearchStrategy[None] = st.none()
    return Limits(
        stop_required=stop_required,
        risk_per_trade=draw(on_or_off(fraction("0.001", "0.05")) if stop_required else off),
        min_reward_to_risk=draw(on_or_off(fraction("0.5", "3")) if stop_required else off),
        max_open_positions=draw(on_or_off(st.integers(min_value=1, max_value=10))),
        max_position=draw(on_or_off(fraction("0.01", "0.5"))),
        max_sector=draw(on_or_off(fraction("0.05", "1"))),
        max_gross=draw(on_or_off(fraction("0.2", "1.5"))),
        participation_cap=draw(on_or_off(fraction("0.001", "0.05"))),
        min_order_value=draw(
            on_or_off(st.decimals(min_value=D("0"), max_value=D("20000"), places=0))
        ),
        max_new_entries=draw(on_or_off(st.integers(min_value=1, max_value=6))),
    )


@st.composite
def cases(draw: st.DrawFn) -> tuple[Limits, Portfolio, list[Proposal]]:
    """The default Limits, the lowvol set or generated ones, a portfolio, and up to eight
    Plans."""
    limits = draw(st.one_of(st.just(LIMITS), st.just(LOWVOL), generated_limits()))
    equity = draw(st.decimals(min_value=D("10000"), max_value=D("5000000"), places=2))
    held = tuple(draw(st.lists(holdings(), max_size=9)))
    count = draw(st.integers(min_value=1, max_value=8))
    plans = [draw(proposals(rank, limits)) for rank in range(1, count + 1)]
    return limits, Portfolio(equity, held), plans


def sector(name: str | None) -> str:
    return UNCLASSIFIED if name is None else name


@settings(max_examples=600, deadline=None)
@given(cases())
def test_no_pass_breaches_any_limit_that_is_on_given_the_portfolio_and_the_passes_before_it(
    case: tuple[Limits, Portfolio, list[Proposal]],
) -> None:
    limits, portfolio, asked = case
    equity = portfolio.equity
    decisions = assess(asked, portfolio=portfolio, limits=limits, run_id=RUN)
    assert [d.plan.rank for d in decisions] == [p.plan.rank for p in asked]

    open_positions = len(portfolio.holdings)
    gross = sum((h.value for h in portfolio.holdings), D(0))
    by_sector: dict[str, Decimal] = {}
    for h in portfolio.holdings:
        by_sector[sector(h.sector)] = by_sector.get(sector(h.sector), D(0)) + h.value
    passes = 0
    for asked_for, decision in zip(asked, decisions, strict=True):
        if isinstance(decision, Block):
            continue
        plan, value = decision.plan, decision.plan.entry * decision.plan.shares
        name = sector(asked_for.sector)
        assert plan.shares > 0
        if isinstance(plan, PortfolioPlan):
            assert not limits.stop_required
            assert isinstance(asked_for.plan, PortfolioPlan)
            assert plan.shares <= asked_for.plan.shares
        else:
            assert isinstance(asked_for.plan, Plan)
            assert plan.stop > 0 and plan.stop < plan.entry
            assert plan.stop % asked_for.tick == 0 and plan.target % asked_for.tick == 0
            assert plan.stop <= asked_for.plan.stop
            if asked_for.plan.stop < plan.entry:
                # The asked multiple of the rounded risk, rounded up by under a tick.
                multiple = Fraction(asked_for.plan.target - plan.entry) / Fraction(
                    plan.entry - asked_for.plan.stop
                )
                reach = Fraction(plan.entry) + multiple * Fraction(plan.entry - plan.stop)
                assert reach <= plan.target < reach + Fraction(asked_for.tick)
            else:
                assert plan.target <= asked_for.plan.target
            if isinstance(limits.min_reward_to_risk, Decimal):
                reward_to_risk = (plan.target - plan.entry) / (plan.entry - plan.stop)
                assert reward_to_risk >= limits.min_reward_to_risk
            if isinstance(limits.risk_per_trade, Decimal):
                assert plan.shares * (plan.entry - plan.stop) <= equity * limits.risk_per_trade
        if isinstance(limits.max_position, Decimal):
            assert value <= equity * limits.max_position
        if isinstance(limits.max_sector, Decimal):
            assert by_sector.get(name, D(0)) + value <= equity * limits.max_sector
        if isinstance(limits.max_gross, Decimal):
            assert gross + value <= equity * limits.max_gross
        if isinstance(limits.participation_cap, Decimal):
            assert value <= asked_for.average_traded_value * limits.participation_cap
        if isinstance(limits.min_order_value, Decimal):
            assert value >= limits.min_order_value
        if isinstance(limits.max_new_entries, int):
            assert passes + 1 <= limits.max_new_entries
        if isinstance(limits.max_open_positions, int):
            assert open_positions + 1 <= limits.max_open_positions
        passes += 1
        open_positions += 1
        gross += value
        by_sector[name] = by_sector.get(name, D(0)) + value
