"""The metrics a backtest is scored by, on fixed series worked by hand (no database).

Sharpe: the curve 100, 110, 99, 108.9 returns +10%, -10%, +10%. Over no
risk-free rate the mean is 1/30 and the sample standard deviation 0.2 / sqrt(3),
so the daily ratio is sqrt(3) / 6 and times sqrt(252) it is sqrt(21), 4.5826.
At 25.2% a year the daily rate is 0.1%, the mean excess return 0.097 / 3, and
the Sharpe 0.097 x sqrt(756) / 0.6 = 4.4451.
"""

from datetime import date
from decimal import Decimal

from tradeline.metrics import (
    HaltDay,
    Trade,
    expectancy,
    halt_days,
    max_drawdown,
    profit_factor,
    sharpe,
)

D = Decimal


def test_sharpe_is_the_mean_daily_excess_return_over_its_deviation_times_root_252() -> None:
    curve = [D(100), D(110), D(99), D("108.9")]

    assert round(sharpe(curve, risk_free_rate=D(0)) or 0, 4) == D("4.5826")
    assert round(sharpe(curve, risk_free_rate=D("0.252")) or 0, 4) == D("4.4451")


def test_sharpe_is_undefined_for_a_flat_curve_or_one_with_fewer_than_two_returns() -> None:
    assert sharpe([D(100), D(100), D(100)], risk_free_rate=D("0.065")) is None
    assert sharpe([D(100), D(101)], risk_free_rate=D("0.065")) is None


def test_max_drawdown_is_the_largest_fall_from_a_peak_as_a_fraction_of_it() -> None:
    assert max_drawdown([D(100), D(120), D(90), D(130), D(104)]) == D("0.25")
    assert max_drawdown([D(100), D(101), D(102)]) == D(0)


def test_profit_factor_is_gross_profit_over_gross_loss_and_undefined_without_a_loser() -> None:
    assert profit_factor([D(300), D(-100), D(200), D(-50)]) == D(500) / D(150)
    assert profit_factor([D(300), D(0)]) is None


def test_expectancy_is_the_mean_net_pnl_per_trade_in_rupees_and_as_a_fraction_of_equity() -> None:
    trades = [Trade(D(300), D(100000)), Trade(D(-100), D(50000))]

    found = expectancy(trades)

    assert found is not None
    assert (found.rupees, found.fraction) == (D(100), (D("0.003") + D("-0.002")) / 2)
    assert expectancy([]) is None


def test_a_halt_day_is_one_whose_loss_on_the_day_before_or_drawdown_reaches_its_limit() -> None:
    days = [date(2024, 1, d) for d in (1, 2, 3, 4, 5)]
    # Peak 100 on day 2; day 3 loses 3% of 100; day 4 is 10% under the peak; day 5 recovers.
    curve = [D(98), D(100), D(97), D(90), D(95)]

    found = halt_days(days, curve, daily_loss=D("0.03"), drawdown=D("0.10"))

    assert found == [
        HaltDay(date(2024, 1, 3), ("daily_loss",), D("0.03"), D("0.03")),
        HaltDay(date(2024, 1, 4), ("daily_loss", "drawdown"), D(7) / D(97), D("0.1")),
    ]
