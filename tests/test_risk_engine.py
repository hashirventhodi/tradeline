"""The Risk Engine over hand-built portfolios and Plans, with no database.

Unless a test says otherwise the Limits are ``docs/risk-controls.md``'s
defaults, Equity is Rs 5,00,000 and the tick is 5 paisa: 1 percent of Equity
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

from tradeline.risk import (
    UNCLASSIFIED,
    Block,
    Holding,
    Limits,
    Pass,
    PassRejected,
    Portfolio,
    Proposal,
    assess,
    verify_pass,
)
from tradeline.strategy import Plan
from tradeline.strategy.pricing import PricedPlan

D = Decimal
EQUITY = D("500000")
LIMITS = Limits()
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
def proposals(draw: st.DrawFn, rank: int) -> Proposal:
    entry = draw(RUPEES)
    stop = entry - draw(st.decimals(min_value=D("-1"), max_value=entry, places=3))
    target = entry + draw(st.decimals(min_value=D("0"), max_value=entry, places=3))
    plan = Plan(rank, f"P{rank}", f"P{rank}", entry, stop, target, 10, {})
    tick = draw(st.sampled_from([D("0.01"), D("0.05"), D("0.10")]))
    traded_value = draw(st.decimals(min_value=D("0"), max_value=D("1e9"), places=0))
    return Proposal(plan, tick=tick, sector=draw(SECTORS), average_traded_value=traded_value)


def fraction(low: str, high: str) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=D(low), max_value=D(high), places=3)


@st.composite
def generated_limits(draw: st.DrawFn) -> Limits:
    return Limits(
        risk_per_trade=draw(fraction("0.001", "0.05")),
        max_open_positions=draw(st.integers(min_value=1, max_value=10)),
        max_position=draw(fraction("0.01", "0.5")),
        max_sector=draw(fraction("0.05", "1")),
        max_gross=draw(fraction("0.2", "1.5")),
        participation_cap=draw(fraction("0.001", "0.05")),
        min_order_value=draw(st.decimals(min_value=D("0"), max_value=D("20000"), places=0)),
        max_new_entries=draw(st.integers(min_value=1, max_value=6)),
        min_reward_to_risk=draw(fraction("0.5", "3")),
    )


@st.composite
def cases(draw: st.DrawFn) -> tuple[Limits, Portfolio, list[Proposal]]:
    """The default Limits or generated ones, a portfolio, and up to eight Plans."""
    equity = draw(st.decimals(min_value=D("10000"), max_value=D("5000000"), places=2))
    held = tuple(draw(st.lists(holdings(), max_size=9)))
    count = draw(st.integers(min_value=1, max_value=8))
    plans = [draw(proposals(rank)) for rank in range(1, count + 1)]
    return draw(st.one_of(st.just(LIMITS), generated_limits())), Portfolio(equity, held), plans


def sector(name: str | None) -> str:
    return UNCLASSIFIED if name is None else name


@settings(max_examples=600, deadline=None)
@given(cases())
def test_no_pass_breaches_any_limit_given_the_portfolio_and_the_passes_before_it(
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
        assert (plan.target - plan.entry) / (plan.entry - plan.stop) >= limits.min_reward_to_risk
        assert plan.shares * (plan.entry - plan.stop) <= equity * limits.risk_per_trade
        assert value <= equity * limits.max_position
        assert by_sector.get(name, D(0)) + value <= equity * limits.max_sector
        assert gross + value <= equity * limits.max_gross
        assert value <= asked_for.average_traded_value * limits.participation_cap
        assert value >= limits.min_order_value
        assert passes + 1 <= limits.max_new_entries
        assert open_positions + 1 <= limits.max_open_positions
        passes += 1
        open_positions += 1
        gross += value
        by_sector[name] = by_sector.get(name, D(0)) + value
