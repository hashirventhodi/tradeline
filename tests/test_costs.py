"""The cost model against hand-computed round trips from the checked-in rate schedule.

No real Zerodha contract note was available to the build (#72), so these round
trips are the cost model's stated check. Each is worked by hand from
``cost_rates.csv`` and Zerodha's rounding (its brokerage calculator rounds STT
and stamp duty to the rupee, every other charge to the paisa), one Fill being
one contract note line:

Buy 100 at Rs 1,234.50 (value Rs 1,23,450.00), sell 100 at Rs 1,300.00 (value
Rs 1,30,000.00).

- 2024-05: exchange 0.00322%, DP Rs 13.50.
  Buy: STT 123.45 -> 123; stamp 18.5175 -> 19; exchange 3.97509 -> 3.98; SEBI
  0.12345 -> 0.12; GST 18% of 4.10 = 0.738 -> 0.74. Rs 146.84.
  Sell: STT 130; exchange 4.186 -> 4.19; SEBI 0.13; GST 18% of 4.32 = 0.7776 ->
  0.78; DP 13.50 and its GST 2.43. Rs 151.03. Round trip Rs 297.87.
- 2025-01: exchange 0.00297%, DP Rs 13.
  Buy: STT 123; stamp 19; exchange 3.666465 -> 3.67; SEBI 0.12; GST 18% of 3.79
  = 0.6822 -> 0.68. Rs 146.47.
  Sell: STT 130; exchange 3.861 -> 3.86; SEBI 0.13; GST 18% of 3.99 = 0.7182 ->
  0.72; DP 13 and its GST 2.34. Rs 150.05. Round trip Rs 296.52.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from synthetic_eod import REPO_ROOT
from tradeline.costs import (
    Charges,
    ContractNote,
    CostModel,
    Slippage,
    load_contract_notes,
    load_cost_schedule,
)

D = Decimal
SCHEDULE = load_cost_schedule(REPO_ROOT / "cost_rates.csv")
# Each Fill at the rates in force on its own day, with no Slippage: a contract note.
AT_TRADE_DATE = CostModel(SCHEDULE, Slippage(margin=D(0), impact=D(0)))
BUY = (D("1234.50"), 100)
SELL = (D("1300.00"), 100)
LIQUID = D("1000000000")


def charges(model: CostModel, side: str, day: date, price_shares: tuple[Decimal, int]) -> Charges:
    price, shares = price_shares
    return model.charges(side, day=day, price=price, shares=shares, average_traded_value=LIQUID)


def test_a_round_trip_before_2024_10_costs_what_the_schedule_in_force_on_each_day_says() -> None:
    buy = charges(AT_TRADE_DATE, "buy", date(2024, 5, 2), BUY)
    sell = charges(AT_TRADE_DATE, "sell", date(2024, 5, 15), SELL)

    assert buy == Charges(
        stt=D("123"),
        stamp_duty=D("19"),
        exchange_charge=D("3.98"),
        sebi_fee=D("0.12"),
        gst=D("0.74"),
        dp_charge=D("0"),
        dp_gst=D("0"),
        slippage=D("0"),
    )
    assert sell == Charges(
        stt=D("130"),
        stamp_duty=D("0"),
        exchange_charge=D("4.19"),
        sebi_fee=D("0.13"),
        gst=D("0.78"),
        dp_charge=D("13.50"),
        dp_gst=D("2.43"),
        slippage=D("0"),
    )
    assert (buy.total, sell.total, buy.total + sell.total) == (
        D("146.84"),
        D("151.03"),
        D("297.87"),
    )


def test_a_round_trip_after_2024_10_costs_what_the_schedule_in_force_on_each_day_says() -> None:
    buy = charges(AT_TRADE_DATE, "buy", date(2025, 1, 6), BUY)
    sell = charges(AT_TRADE_DATE, "sell", date(2025, 1, 20), SELL)

    assert (buy.exchange_charge, buy.gst, buy.total) == (D("3.67"), D("0.68"), D("146.47"))
    assert (sell.exchange_charge, sell.gst, sell.dp_charge, sell.dp_gst, sell.total) == (
        D("3.86"),
        D("0.72"),
        D("13"),
        D("2.34"),
        D("150.05"),
    )
    assert buy.total + sell.total == D("296.52")


def test_a_backtest_prices_every_trade_at_the_schedule_of_one_stated_date() -> None:
    """The schedule as of 2025-01-06 prices a 2024-05 round trip at 0.00297% and Rs 13."""
    frozen = CostModel(SCHEDULE, Slippage(margin=D(0), impact=D(0)), rates_as_of=date(2025, 1, 6))

    buy = charges(frozen, "buy", date(2024, 5, 2), BUY)
    sell = charges(frozen, "sell", date(2024, 5, 15), SELL)

    assert buy.total + sell.total == D("296.52")


def test_slippage_is_a_safety_margin_plus_impact_on_the_orders_share_of_traded_value() -> None:
    """Rs 1,23,450 against Rs 1,23,45,000 a day traded: 0.05% + 0.1 x 1% = 0.15%, Rs 185.175
    to the paisa; at no impact, 0.05%, Rs 61.725."""
    model = CostModel(SCHEDULE, Slippage(margin=D("0.0005"), impact=D("0.1")))
    flat = CostModel(SCHEDULE, Slippage(margin=D("0.0005"), impact=D("0")))

    def slippage(m: CostModel) -> Decimal:
        price, shares = BUY
        found = m.charges(
            "buy",
            day=date(2025, 1, 6),
            price=price,
            shares=shares,
            average_traded_value=D("12345000"),
        )
        return found.slippage

    assert slippage(model) == D("185.18")
    assert slippage(flat) == D("61.73")


def test_stt_and_stamp_duty_round_half_up_to_the_rupee() -> None:
    """Rs 1,00,500: STT 100.5 -> 101, stamp 15.075 -> 15. Rs 1,03,500: stamp 15.525 -> 16."""
    buy = charges(AT_TRADE_DATE, "buy", date(2025, 1, 6), (D("1005"), 100))
    more = charges(AT_TRADE_DATE, "buy", date(2025, 1, 6), (D("1035"), 100))
    assert (buy.stt, buy.stamp_duty, more.stamp_duty) == (D("101"), D("15"), D("16"))


def test_a_trade_before_the_schedules_first_date_is_refused() -> None:
    with pytest.raises(ValueError, match="before the cost schedule's first date 2023-04-01"):
        charges(AT_TRADE_DATE, "buy", date(2023, 3, 31), BUY)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("2023-04-01,stt,0.001,", "no citation"),
        ("2023-04-01,brokerage,0.001,X", "unknown charge 'brokerage'"),
        ("2023-04-01,stt,-1,X", "is not a non-negative number"),
        ("2023-04-01,stt,0.001,X\n2023-04-01,stt,0.002,X", "stt on 2023-04-01 again"),
        ("2023-04-01,stt,0.001,X", "2023-04-01 has no rate for stamp_duty"),
    ],
)
def test_a_bad_schedule_row_refuses_the_whole_file(tmp_path: Path, row: str, message: str) -> None:
    path = tmp_path / "rates.csv"
    path.write_text(f"in_force_from,charge,rate,citation\n{row}\n")

    with pytest.raises(ValueError, match=message):
        load_cost_schedule(path)


# Real contract notes the Operator supplies (none yet: the report says so, #79).
CONTRACT_NOTES = load_contract_notes(REPO_ROOT / "contract_notes.csv")


@pytest.mark.parametrize("note", CONTRACT_NOTES, ids=lambda n: n.source)
def test_each_real_contract_note_matches_the_rates_in_force_on_its_trade_date(
    note: ContractNote,
) -> None:
    found = AT_TRADE_DATE.charges(
        note.side,
        day=note.trade_date,
        price=note.price,
        shares=note.shares,
        average_traded_value=LIQUID,
    )
    assert (found.stt, found.stamp_duty, found.exchange_charge, found.sebi_fee, found.gst) == (
        note.stt,
        note.stamp_duty,
        note.exchange_charge,
        note.sebi_fee,
        note.gst,
    )
