"""The single-run report: ``tradeline backtest report`` over the synthetic markets, read back
from its JSON (and, for Sharpe's curve, the query layer).

The two-year report is ``synthetic_eod.two_year_market`` from 2022-07-01 to
2024-06-28 under a Protocol frozen over it (``synthetic_eod.freeze_protocol``),
as ``test_positions.py`` backtests it (k = 2, r = 2, n = 10, Rs 5,00,000, the
cost schedule as of 2026-10-24, c = 0.1, risk-free 6.5 percent), with the Halts
(configuration, not the Protocol's) lowered
to a 0.8 percent daily loss and a 1.4 percent drawdown so that some fire. Its
six closed trades, worked in ``test_positions.py``, net Rs 945.79, 2,553.64,
3,776.42, 2,603.77 and -5,589.17 (the differences of the Runs' Equity there),
and BIRCH's last, 76 shares bought at 991.75 and sold at 971.75, nets -1,520
less the buy's charges (STT 75, stamp 11, exchange 2.31, SEBI 0.08, GST 0.43,
Slippage 37.6865 + 0.1 x 75,373^2 / 9,83,25,000 -> 43.46: Rs 132.28) and the
sale's (Rs 134.58): -1,786.86. So:

- profit factor 9,879.62 / 7,376.03 = 1.3394;
- Expectancy 2,503.59 / 6 = Rs 417.265, reported to the paisa half-even as
  417.26; as fractions of the Equity each Pass sized against (5,00,000,
  5,00,945.79, 5,03,499.43, 5,07,275.85, 5,09,879.62, 5,04,290.45) they average
  0.000853;
- the Equity peak is 2023-03-03, ASTER held at its close 847: cash 5,07,275.85
  less 93 x 816 and the buy's Rs 134.92 is 4,31,252.93, plus 93 x 847 =
  5,10,023.93. The deepest point is the last day, 5,02,503.59 (5,04,290.45 less
  BIRCH's 1,786.86): a drawdown of 7,520.34 / 5,10,023.93 = 0.014745.

Gapped Stops are the two stop exits filled under the stop: ASTER in 2024-01
(stop 1040, filled at 978) and BIRCH in 2024-03 (stop 979.75, Circuit Lock,
filled at 971.75). The daily-loss Halt fires on 2024-01-08, ASTER's 72 shares
marked down from 1039 to 978: bought for 72 x 1052 and Rs 133.21 of charges, they
leave 4,34,002.41 in cash, so Equity falls Rs 4,392 from 5,08,810.41 (0.008632)
to 5,04,418.41, 5,605.52 under the peak (0.010991). The drawdown Halt fires first on
2024-03-14, BIRCH locked at 972.75: Equity 5,04,290.45 - 76 x 991.75 - 132.28 +
76 x 972.75 = 5,02,714.17, 7,309.76 under the peak (0.014332), and on every day
after.

The short reports are ``synthetic_eod.base_market`` over ``RUN_DAY`` and the
day after, under a Protocol frozen over the quarter to June 2023, as
``test_verdicts.py`` works RUN_DAY: ALPHA and GAMMA Pass, BETA is Blocked by
the sector Limit in Unclassified; both entries fill on 2023-06-16 at their
limits and are open at the end. Under a Protocol whose Limits allow one new
entry a Run, GAMMA and BETA are Candidates beyond the Passes instead.
"""

import contextlib
import io
import json
import statistics
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from synthetic_eod import (
    REPO_ROOT,
    RUN_DAY,
    TWO_YEARS,
    Market,
    base_market,
    freeze_protocol,
    load_market,
    repo_calendar,
    two_year_market,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.risk import Limits

D = Decimal
# The stated CI budget for the fixture backtest: two years (494 trading days) of three
# Universe members, one cell, its Runs, Orders, Fills and Positions written to the Ledger.
FIXTURE_BUDGET_SECONDS = 20.0
# The short reports' Protocol: In-Sample April, Out-of-Sample May, Holdout June 2023.
QUARTER = (date(2023, 4, 1), date(2023, 6, 30))


def config_text(market: Market, extra_risk: str = "") -> str:
    rule = market.rule
    return (
        f"[universe]\ntop_n = {rule.top_n}\nlookback_trading_days = {rule.lookback_trading_days}\n"
        f"min_listing_trading_days = {rule.min_listing_trading_days}\n"
        f"min_presence_percent = {rule.min_presence_percent}\n"
        f'[integrity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[parity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[risk]\ntick_sizes = "{REPO_ROOT / "tick_sizes.csv"}"\n{extra_risk}'
        f'[costs]\nschedule = "{REPO_ROOT / "cost_rates.csv"}"\n'
        f'contract_notes = "{REPO_ROOT / "contract_notes.csv"}"\n'
    )


def report_over(
    database_url: str,
    directory: Path,
    market: Market,
    start: date,
    end: date,
    *,
    frozen_over: tuple[date, date] | None = None,
    limits: Limits | None = None,
    extra_risk: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the market, freeze a Protocol over ``frozen_over`` (``start`` to ``end`` unless
    given) with ``limits``, and report it from ``start`` to ``end``; the report's JSON and
    the command's last log line."""
    fixtures = write_fixtures(market, directory / "fixtures", repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        frozen = freeze_protocol(conn, market, *(frozen_over or (start, end)), limits=limits)
    config = directory / "tradeline.toml"
    config.write_text(config_text(market, extra_risk))
    out = directory / "report.json"
    logs = io.StringIO()
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(logs):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        arguments = ["--from", start.isoformat(), "--to", end.isoformat(), "--out", str(out)]
        assert main(["backtest", "report", *arguments, "--protocol", frozen.hash]) == 0
    finished = json.loads(logs.getvalue().splitlines()[-1])
    assert finished["msg"] == "backtest report finished"
    found: dict[str, Any] = json.loads(out.read_text())
    return found, finished


@pytest.fixture(scope="module")
def two_years(
    database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> tuple[dict[str, Any], dict[str, Any], str]:
    report, finished = report_over(
        database_url,
        tmp_path_factory.mktemp("two_years"),
        two_year_market(),
        *TWO_YEARS,
        extra_risk="daily_loss_halt = 0.008\ndrawdown_halt = 0.014\n",
    )
    return report, finished, database_url


def test_the_report_states_the_metrics_of_the_fixture_backtest_as_worked_by_hand(
    two_years: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    report = two_years[0]

    assert report["trading_days"] == 494
    assert {k: v for k, v in report["metrics"].items() if k != "sharpe"} == {
        "trades": 6,
        "max_drawdown": "0.014745",
        "profit_factor": "1.3394",
        "expectancy": {"rupees": "417.26", "fraction_of_equity": "0.000853"},
        "ending_equity": "502503.59",
        "open_positions": 0,
    }
    assert [t["net_pnl"] for t in report["closed_trades"]] == [
        "945.79",
        "2553.64",
        "3776.42",
        "2603.77",
        "-5589.17",
        "-1786.86",
    ]
    assert report["open_positions"] == []
    assert report["contract_notes"] == "none available"


def test_sharpe_is_the_daily_excess_return_of_the_runs_equity_over_its_deviation(
    two_years: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    report, finished, database_url = two_years
    with app_connection(database_url) as conn:
        runs = queries.runs(conn, mode="backtest", backtest_id=uuid.UUID(finished["backtest_id"]))
    curve = [float(r.equity or 0) for r in runs]
    excess = [b / a - 1 - 0.065 / 252 for a, b in zip(curve, curve[1:], strict=False)]

    expected = statistics.mean(excess) / statistics.stdev(excess) * 252**0.5

    assert len(curve) == 494
    assert float(report["metrics"]["sharpe"]) == pytest.approx(expected, abs=0.0001)
    assert report["risk_free_rate"] == "0.065000"


def test_gapped_stops_are_listed_with_their_realised_loss(
    two_years: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    assert two_years[0]["gapped_stops"] == [
        {
            "symbol": "ASTER",
            "opened_on": "2024-01-04",
            "closed_on": "2024-01-09",
            "shares": 72,
            "entry": "1052.00",
            "stop": "1040.00",
            "exit_price": "978.00",
            "planned_risk": "864.00",
            "realised_loss": "5589.17",
        },
        {
            "symbol": "BIRCH",
            "opened_on": "2024-03-13",
            "closed_on": "2024-03-18",
            "shares": 76,
            "entry": "991.75",
            "stop": "979.75",
            "exit_price": "971.75",
            "planned_risk": "912.00",
            "realised_loss": "1786.86",
        },
    ]


def test_every_day_a_halt_would_have_fired_is_listed_with_the_drawdown_then(
    two_years: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    halts = two_years[0]["halt_would_have_fired"]
    days = halts["days"]

    assert (halts["daily_loss_limit"], halts["drawdown_limit"]) == ("0.008000", "0.014000")
    assert days[0] == {
        "day": "2024-01-08",
        "limits": ["daily_loss"],
        "daily_loss": "0.008632",
        "drawdown": "0.010991",
    }
    assert (days[1]["day"], days[1]["limits"], days[1]["drawdown"]) == (
        "2024-03-14",
        ["drawdown"],
        "0.014332",
    )
    # The drawdown Halt holds from 2024-03-14 to the last day: 71 trading days.
    assert [d["day"] for d in days[1:]][-1] == "2024-06-28"
    assert len(days) == 1 + 71
    assert all(d["limits"] == ["drawdown"] for d in days[1:])


def test_the_report_states_the_results_with_c_at_zero_and_at_twice_its_value(
    two_years: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    report = two_years[0]
    zero, double = report["cost_sensitivity"]

    assert report["costs"] == {
        "rates_as_of": "2026-10-24",
        "safety_margin": "0.0005",
        "impact": "0.1",
    }
    assert (zero["impact"], double["impact"]) == ("0", "0.2")
    assert zero["trades"] == double["trades"] == 6
    # Less impact, more left: each Fill's Slippage is the margin plus c x its share of
    # traded value.
    ending = [D(x["ending_equity"]) for x in (zero, report["metrics"], double)]
    assert ending == sorted(ending, reverse=True)
    assert len(set(ending)) == 3
    factors = [D(x["profit_factor"]) for x in (zero, report["metrics"], double)]
    assert factors == sorted(factors, reverse=True)
    # Each variant's Runs record the c they were priced with; the cell's are the Protocol's.
    finished, database_url = two_years[1], two_years[2]
    labelled = [("cell", finished["backtest_id"]), *finished["variant_ids"].items()]
    with app_connection(database_url) as conn:
        impacts = {
            label: {r.impact for r in queries.runs(conn, backtest_id=uuid.UUID(found))}
            for label, found in labelled
        }
    assert impacts == {"cell": {None}, "0": {D(0)}, "0.2": {D("0.2")}}


def test_the_fixture_backtest_runs_within_its_stated_budget(
    two_years: tuple[dict[str, Any], dict[str, Any], str],
) -> None:
    assert two_years[1]["seconds"] < FIXTURE_BUDGET_SECONDS


def test_open_positions_at_the_end_are_marked_and_listed_and_unclassified_blocks_counted(
    fresh_database_url: str, tmp_path: Path
) -> None:
    report, _ = report_over(
        fresh_database_url,
        tmp_path,
        base_market(),
        RUN_DAY,
        date(2023, 6, 16),
        frozen_over=QUARTER,
    )

    assert report["metrics"]["trades"] == 0
    assert report["closed_trades"] == []
    assert [
        (p["symbol"], p["opened_on"], p["shares"], p["entry"]) for p in report["open_positions"]
    ] == [("ALPHA", "2023-06-16", 267, "280.00"), ("GAMMA", "2023-06-16", 133, "560.00")]
    assert report["metrics"]["open_positions"] == 2
    assert report["unclassified_blocks"] == 1
    assert report["candidates_beyond_passes"] == 0
    assert report["blocks_by_limit"] == {"max_sector": 1}


def test_the_report_counts_the_candidates_beyond_the_passes(
    fresh_database_url: str, tmp_path: Path
) -> None:
    report, _ = report_over(
        fresh_database_url,
        tmp_path,
        base_market(),
        RUN_DAY,
        RUN_DAY,
        frozen_over=QUARTER,
        limits=Limits(max_new_entries=1),
    )

    assert report["candidates_beyond_passes"] == 2
    assert report["unclassified_blocks"] == 0
    assert report["blocks_by_limit"] == {"max_new_entries": 2}
