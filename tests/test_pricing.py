"""Plan pricing and the dated table of NSE tick sizes, with no database.

The table is the checked-in ``tick_sizes.csv``. NSE/CMTR/62174 (24 May 2024)
moved every equity under Rs 250 from the 5 paisa tick to 1 paisa from the
trade date of 10 June 2024, and NSE/CMTR/67133 (13 March 2025) added coarser
ticks above Rs 1,000 from 15 April 2025. Each month's tick is set by the
security's close on the last trading day of the month before. The expected
values below are worked by hand from those circulars and from a Plan's stated
prices.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tradeline.strategy import Plan
from tradeline.strategy.pricing import price_plan
from tradeline.tick_sizes import load_tick_sizes

REPO_ROOT = Path(__file__).resolve().parents[1]
TICKS = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
D = Decimal

PLAN = Plan(
    rank=1,
    instrument="ALPHA",
    symbol="ALPHA",
    entry=D("200.00"),
    stop=D("193.333"),
    target=D("213.337"),
    time_stop_days=10,
    facts={},
)


@pytest.mark.parametrize(
    ("day", "reference", "tick"),
    [
        (date(2015, 1, 1), D("10.00"), D("0.05")),
        (date(2024, 6, 7), D("249.95"), D("0.05")),  # the last day before NSE/CMTR/62174
        (date(2024, 6, 10), D("249.95"), D("0.01")),
        (date(2024, 6, 10), D("250.00"), D("0.05")),
        (date(2024, 6, 10), D("4000.00"), D("0.05")),
        (date(2025, 4, 11), D("4000.00"), D("0.05")),  # the last day before NSE/CMTR/67133
        (date(2025, 4, 15), D("249.99"), D("0.01")),
        (date(2025, 4, 15), D("1000.00"), D("0.05")),
        (date(2025, 4, 15), D("1000.05"), D("0.10")),
        (date(2025, 4, 15), D("5000.00"), D("0.10")),
        (date(2025, 4, 15), D("10000.00"), D("0.50")),
        (date(2025, 4, 15), D("20000.00"), D("1.00")),
        (date(2025, 4, 15), D("20000.05"), D("5.00")),
    ],
)
def test_the_tick_in_force_is_the_bands_of_the_latest_circular_for_the_reference_close(
    day: date, reference: Decimal, tick: Decimal
) -> None:
    assert TICKS.tick(day, reference_close=reference) == tick


def test_a_day_before_the_table_has_no_tick() -> None:
    with pytest.raises(ValueError, match="before the tick table"):
        TICKS.tick(date(2014, 12, 31), reference_close=D("100"))


def test_pricing_rounds_the_stop_and_shares_down_and_the_target_up_on_the_5_paisa_tick() -> None:
    priced = price_plan(PLAN, tick=D("0.05"), equity=D("500000"), risk_per_trade=D("0.01"))

    # The target keeps the Plan's multiple of the risk, 13.337 / 6.667, over the rounded
    # risk of 6.70: 200 + 13.4030, rounded up.
    assert (priced.entry, priced.stop, priced.target) == (D("200.00"), D("193.30"), D("213.45"))
    # 5,000 rupees at risk over 6.70 a share is 746.3 shares.
    assert priced.shares == 746
    # (213.45 - 200) / (200 - 193.30)
    assert priced.reward_to_risk == D("13.45") / D("6.70")


def test_pricing_rounds_to_the_1_paisa_tick_below_rs_250_from_june_2024() -> None:
    tick = TICKS.tick(date(2024, 7, 1), reference_close=D("199.00"))
    priced = price_plan(PLAN, tick=tick, equity=D("500000"), risk_per_trade=D("0.01"))

    # 200 + 6.67 x 13.337 / 6.667 = 213.3430, rounded up.
    assert (priced.stop, priced.target) == (D("193.33"), D("213.35"))
    # 5,000 over 6.67 is 749.6.
    assert priced.shares == 749


def test_rounding_never_takes_a_plan_under_its_own_reward_to_risk() -> None:
    # Unrounded: (210.04 - 200) / (200 - 193.31) = 10.04 / 6.69, just over 1.5. Rounding
    # both prices down, as pricing did until #85, gave 10.00 / 6.70, under it.
    plan = replace(PLAN, stop=D("193.31"), target=D("210.04"))
    priced = price_plan(plan, tick=D("0.05"), equity=D("500000"), risk_per_trade=D("0.01"))

    # 200 + 6.70 x 10.04 / 6.69 = 210.0550, rounded up to 210.10.
    assert (priced.stop, priced.target) == (D("193.30"), D("210.10"))
    assert priced.reward_to_risk == D("10.10") / D("6.70")
    assert priced.reward_to_risk >= D("10.04") / D("6.69")


def test_a_target_already_on_the_tick_at_the_multiple_is_kept() -> None:
    plan = replace(PLAN, stop=D("194.00"), target=D("209.00"))
    priced = price_plan(plan, tick=D("0.05"), equity=D("500000"), risk_per_trade=D("0.01"))

    assert (priced.stop, priced.target) == (D("194.00"), D("209.00"))
    assert priced.reward_to_risk == D("1.5")
