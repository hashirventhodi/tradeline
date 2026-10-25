"""The Benchmark Gate and the report's additions (#98), end to end: ``lowvol_v1``'s Protocol
frozen from ``[lowvol]`` (its Gate against the Benchmark, tax, dividends and demergers on),
its Walk-Forward, its Holdout and ``tradeline backtest report --protocol P``.

The market is ``synthetic_eod.lowvol_market`` run on to 2023-05-31, as in
``test_account_tax``: the Stitched Curve is 2022-04-01 to 2023-04-28 (Initial Buy decided
2022-03-31), the Holdout 2023-05. Its Rebalances are decided on 2022-09-30 and
2023-03-31, two, short of the Gate's 8. Added to it, each worked by hand:

- H01 to H10 are in NSE's sector FMCG, the rest in none (Unclassified). The Initial Buy
  buys H01 to H30 at equal shares, so FMCG holds about a third of Equity from 2022-04-01.
- H20 gaps Rs 4,000 up on 2022-08-01 (``lowvol_market``): about 41 times its cost, so from
  then to its sale on 2022-10-03 it is the largest name, near 41 / (29 + 41) of the
  holdings.
- H01 pays "Dividend - Rs 2 Per Share" ex 2022-06-15 and "Dividend - 25%" (of a face
  value of 10) ex 2022-12-15; H02's "Dividend - Rs Per Share" ex 2022-07-15 does not
  parse and is listed, with its reason, for the pre-Holdout check.
- H04 demerges HFOUR, 1 for 10, ex 2022-12-01; HFOUR lists in BE on 2022-12-15 at Rs 50
  and never moves, so H04's holder is credited a tenth of its shares times Rs 50.
- H03 has a rights issue ex 2022-11-15, opening Re 1 under the close before and back the
  next day.

The Benchmark's NAV rises 1 paisa a trading day from 20; the factor fund's 2 paisa from
20, split 1 for 10 from 2022-12-01 (``fund_unit_splits`` of the test).
"""

import contextlib
import io
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from synthetic_eod import (
    BENCHMARK_SCHEME,
    FACTOR_FUND,
    REPO_ROOT,
    Absent,
    Demerger,
    Dividend,
    Gap,
    Rights,
    Stock,
    compute_universe_months,
    config_text,
    load_benchmark,
    load_index_history,
    load_market,
    load_sectors_on,
    lowvol_market,
    repo_calendar,
    write_amfi_fixtures,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.costs import load_cost_schedule
from tradeline.db import app_connection
from tradeline.demergers import load_demergers
from tradeline.dividends import ListedDividend
from tradeline.holdout_checks import TaxTestOutcome, check_holdout
from tradeline.ingest.demergers import ingest_demergers
from tradeline.protocol import REBALANCES_COUNTED, BenchmarkGate

D = Decimal
CALENDAR = repo_calendar()
END = date(2023, 5, 31)
FIRST_DAY = date(2022, 4, 1)
STARTING_EQUITY = D(500000)
SPLIT = date(2022, 12, 1)
UNPARSED = ListedDividend("H02", date(2022, 7, 15), "Dividend - Rs Per Share", "states no amount")


def _market() -> Any:
    market = lowvol_market(END)
    for j in range(1, 11):
        market = market.with_stock(f"H{j:02}", sector="FMCG")
    market = market.with_events(
        "H01",
        Dividend(date(2022, 6, 15), "Dividend - Rs 2 Per Share"),
        Dividend(date(2022, 12, 15), "Dividend - 25%", face_value=10),
    )
    market = market.with_events("H02", Dividend(UNPARSED.ex_date, UNPARSED.subject))
    market = market.with_events(
        "H03",
        Rights(date(2022, 11, 15), "Rights 1:5 @ Premium Rs 50/-"),
        Gap(date(2022, 11, 15), D("-1.00")),
        Gap(date(2022, 11, 16), D("1.00")),
    )
    market = market.with_events("H04", Demerger(SPLIT))
    child = Stock(
        "HFOUR",
        "INE0HFOUR016",
        start=D("50.00"),
        slope=D(0),
        half_range=D("0.50"),
        volume=100_000,
        listed=date(2022, 12, 15),
        series="BE",
        events=(Absent(market.start, date(2022, 12, 14)),),
    )
    return replace(market, stocks=(*market.stocks, child))


def _navs(step: str, *, split: date | None = None) -> dict[date, Decimal]:
    days = CALENDAR.trading_days(date(2022, 3, 1), END)
    navs = {}
    for i, day in enumerate(days):
        nav = D(20) + D(step) * i
        navs[day] = nav / 10 if split is not None and day >= split else nav
    return navs


def _config(directory: Path) -> Path:
    splits = directory / "fund_unit_splits.csv"
    splits.write_text(f"scheme_code,ex_date,ratio,citation\n{FACTOR_FUND},{SPLIT},10,a test\n")
    config = directory / "tradeline.toml"
    config.write_text(
        config_text(_market())
        + f'[tax]\nschedule = "{REPO_ROOT / "tax_rates.csv"}"\n'
        + f'[benchmark]\nfunds = "{REPO_ROOT / "benchmark_funds.csv"}"\n'
        + f'unit_splits = "{splits}"\n'
        + '[lowvol.protocol]\nholdout_start = "2023-05"\nholdout_months = 1\nwindows = 13\n'
        "out_of_sample_months = 1\nin_sample_months = 1\n"
    )
    return config


def _cli(database_url: str, config: Path, *argv: str) -> None:
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        assert main(list(argv)) == 0


Reported = tuple[str, queries.FrozenProtocol, Path, dict[str, Any]]


@pytest.fixture(scope="module")
def reported(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Reported:
    """The market loaded, ``lowvol_v1`` frozen, walked forward, its Holdout run and its
    full report written: the database, the Protocol, the directory and the report's JSON."""
    market = _market()
    directory = tmp_path_factory.mktemp("benchmark_gate")
    fixtures = write_fixtures(market, directory / "eod", CALENDAR)
    amfi = write_amfi_fixtures(
        {BENCHMARK_SCHEME: _navs("0.01"), FACTOR_FUND: _navs("0.02", split=SPLIT)},
        directory / "amfi",
        first=date(2019, 9, 11),
        end=END,
    )
    table = directory / "demergers.csv"
    table.write_text(
        "parent,child,ratio,ex_date,listed_on,parent_basis,child_basis,citation\n"
        f"H04,HFOUR,1:10,{SPLIT},2022-12-15,90,10,NSE/CMTR/65845\n"
    )
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        load_sectors_on(conn, fixtures, date(2021, 3, 1))
        compute_universe_months(conn, market, date(2022, 3, 1), END)
        load_benchmark(conn, amfi, end=END)
        load_index_history(conn)
        ingest_demergers(conn, load_demergers(table))
    config = _config(directory)
    _cli(database_url, config, "protocol", "freeze", "--strategy", "lowvol_v1")
    with app_connection(database_url) as conn:
        (frozen,) = queries.protocols(conn)
    _cli(database_url, config, "backtest", "--protocol", frozen.hash)
    with app_connection(database_url) as conn:
        checks = check_holdout(
            conn,
            protocol=frozen.hash,
            contract_notes=(),
            schedule=load_cost_schedule(REPO_ROOT / "cost_rates.csv"),
            listed=(UNPARSED,),
            accept_no_contract_notes="a synthetic Market's test",
            tax_tests=lambda: TaxTestOutcome(True, "taken as passed"),
        )
        assert checks.passed, checks.summary()
    _cli(database_url, config, "backtest", "holdout", "--protocol", frozen.hash)
    out = _report(database_url, config, frozen.hash, directory / "first")
    return database_url, frozen, config, json.loads(out.read_text())


def _report(database_url: str, config: Path, protocol: str, directory: Path) -> Path:
    directory.mkdir()
    out = directory / "report.json"
    _cli(
        database_url,
        config,
        *("backtest", "report", "--protocol", protocol),
        *("--out", str(out), "--markdown", str(directory / "report.md")),
    )
    return out


def _positions(database_url: str, symbol: str) -> list[queries.LedgerPosition]:
    with app_connection(database_url) as conn:
        walked = queries.walk_forward(conn, queries.protocols(conn)[0].hash)
        assert walked is not None
        found = queries.positions(conn, mode="backtest", backtest_id=walked.backtest_id)
    return [p for p in found if p.symbol == symbol]


def test_the_protocol_freezes_the_benchmark_gate_and_credits_dividends_and_demergers(
    reported: Reported,
) -> None:
    _, frozen, _, _ = reported
    terms = frozen.protocol

    assert terms.gate == BenchmarkGate(D("0"), D("0.02"), 8, REBALANCES_COUNTED)
    assert terms.dividends and terms.demergers and terms.tax is not None
    assert "buffer of 60" in terms.notes["buffer_scale"]
    assert terms.benchmark is not None
    (split,) = terms.benchmark.factor_fund_splits
    assert (split.scheme_code, split.ex_date, split.ratio) == (FACTOR_FUND, SPLIT, D(10))


def test_the_gate_checks_excess_and_drawdown_on_both_curves_and_the_rebalance_count(
    reported: Reported,
) -> None:
    _, _, _, report = reported
    gate = report["gate"]

    assert list(report)[:3] == ["protocol", "strategy", "gate"]
    assert gate["thresholds"] == {
        "excess_above": "0",
        "drawdown_margin": "0.02",
        "min_stitched_rebalances": 8,
        "rebalances_counted": REBALANCES_COUNTED,
    }
    assert [(c["curve"], c["metric"], c["rule"]) for c in gate["checks"]] == [
        ("stitched_curve", "annualised_excess", "above"),
        ("stitched_curve", "drawdown", "at most"),
        ("stitched_curve", "rebalances", "at least"),
        ("holdout", "annualised_excess", "above"),
        ("holdout", "drawdown", "at most"),
    ]
    rebalances = gate["checks"][2]
    # Decided on 2022-09-30 and 2023-03-31; the Initial Buy is not one.
    assert (rebalances["value"], rebalances["threshold"], rebalances["passed"]) == (2, 8, False)
    assert gate["outcome"] == "fail"
    assert "stitched_curve rebalances" in gate["reason"]
    assert gate["holdout_rebalances"] == 0
    # The checks read the curves' comparison of Liquidation Values after tax.
    for check in gate["checks"][:2]:
        after = report["stitched_curve"]["against_benchmark"]["after_tax"]
        metric = "excess" if check["metric"] == "annualised_excess" else "drawdown"
        assert check["value"] == after[metric]
    drawdown = gate["checks"][1]
    assert D(drawdown["threshold"]) == D(after["benchmark_drawdown"]) + D("0.02")


def test_the_information_ratio_is_in_the_gate_near_the_top(reported: Reported) -> None:
    _, _, _, report = reported

    assert list(report["gate"])[:3] == ["outcome", "reason", "information_ratio"]
    ratios = report["gate"]["information_ratio"]
    assert (
        ratios["stitched_curve"]
        == (report["stitched_curve"]["against_benchmark"]["after_tax"]["information_ratio"])
    )
    assert list(ratios) == ["stitched_curve", "holdout"]


def test_results_before_tax_add_back_the_tax_paid_and_owed(reported: Reported) -> None:
    _, _, _, report = reported
    stitched = report["stitched_curve"]
    liquidation = stitched["liquidation"]
    before = stitched["against_benchmark"]["before_tax"]
    after = stitched["against_benchmark"]["after_tax"]

    # Debited before the last day; the last day's Liquidation Value owes the rest, the
    # final year its Boundary Exit settles among it.
    last = stitched["to"]
    paid = sum(D(t["tax"]) for t in liquidation["tax_debits"] if t["day"] < last)
    assert paid > 0
    assert D(before["strategy_value"]) == D(liquidation["value"]) + D(liquidation["tax_due"]) + paid
    assert D(after["strategy_value"]) == D(liquidation["value"])
    benchmark = liquidation["benchmark"]
    assert D(before["benchmark_value"]) == D(benchmark["value"]) + D(benchmark["tax_due"])
    assert D(before["excess"]) > D(after["excess"])
    for name in ("sharpe", "profit_factor", "expectancy"):
        assert name in stitched["metrics"]


def test_the_factor_fund_is_compared_at_nav_chained_across_its_unit_split(
    reported: Reported,
) -> None:
    _, _, _, report = reported
    factor = report["stitched_curve"]["factor_fund"]

    # 2 paisa a trading day from 20 on 2022-03-01: Rs 20.42 on 2022-04-01, the first day,
    # and on 2023-04-28 the NAV of 20 + 0.02 x (trading days from 2022-03-01 less one),
    # a tenth of it on the new units and so 10 times that read back.
    days = list(CALENDAR.trading_days(date(2022, 3, 1), date(2023, 4, 28)))
    first = D(20) + D("0.02") * days.index(FIRST_DAY)
    last = D(20) + D("0.02") * (len(days) - 1)
    assert factor["scheme_code"] == FACTOR_FUND
    assert D(factor["value"]) == (STARTING_EQUITY * last / first).quantize(D("0.01"))
    # A rise every day: no drawdown, so the split is no fall.
    assert factor["drawdown"] == "0.000000"
    assert factor["unit_splits"] == [{"ex_date": SPLIT.isoformat(), "ratio": "10"}]


def test_sector_weights_at_every_rebalance_with_the_peak_between(reported: Reported) -> None:
    _, _, _, report = reported
    rebalances = report["stitched_curve"]["portfolio"]["rebalances"]

    assert [(r["step"], r["decided"]) for r in rebalances] == [
        ("initial_buy", "2022-03-31"),
        ("rebalance", "2022-09-30"),
        ("rebalance", "2023-03-31"),
    ]
    initial = rebalances[0]
    assert initial["held_from"] == FIRST_DAY.isoformat()
    weights = {s["sector"]: D(s["weight"]) for s in initial["sectors"]}
    assert set(weights) == {"FMCG", "unclassified"}
    # Ten of thirty equal buys, sized at the limit 1 percent over the close.
    assert D("0.32") < weights["FMCG"] < D("0.34")
    assert D("0.64") < weights["unclassified"] < D("0.67")
    # Between the Initial Buy and the Rebalance H20 (Unclassified) rose 41-fold, so FMCG's
    # peak is about where it started and Unclassified's on H20's day.
    peaks = {s["sector"]: s for s in initial["peak_until_next"]}
    assert D(peaks["FMCG"]["weight"]) >= weights["FMCG"]
    # H20 is held from its gap to its sale on 2022-10-03; the swings set the day.
    assert "2022-08-01" <= peaks["unclassified"]["day"] < "2022-10-03"
    assert D(peaks["unclassified"]["weight"]) > D("0.8")


def test_the_largest_name_weight_is_h20_on_its_gap(reported: Reported) -> None:
    _, _, _, report = reported
    largest = report["stitched_curve"]["portfolio"]["largest_name_weight"]

    assert largest["symbol"] == "H20"
    assert "2022-08-01" <= largest["day"] < "2022-10-03"
    # Its 41-fold value among 29 others at about their cost: near 41 / 70 of Equity.
    assert D("0.55") < D(largest["weight"]) < D("0.6")


def test_cash_at_every_rebalance(reported: Reported) -> None:
    _, _, _, report = reported
    initial, sold_h20, _ = report["stitched_curve"]["portfolio"]["rebalances"]

    # Sizing at the limit leaves up to about 1 percent of each buy in cash.
    assert D(0) < D(initial["cash"]) < D("0.02")
    # H20's sale leaves more than three entrants capped at 15 percent of Equity can take.
    assert D(sold_h20["cash"]) > D("0.10")


def test_dividends_parsed_and_unparsed_are_counted(reported: Reported) -> None:
    database_url, _, _, report = reported
    dividends = report["stitched_curve"]["portfolio"]["dividends"]
    (h01,) = _positions(database_url, "H01")

    assert (dividends["parsed"], dividends["unparsed"]) == (2, 1)
    assert D(dividends["credited"]) == h01.shares * (D(2) + D("2.5"))
    assert dividends["unparsed_subjects"] == [
        {
            "symbol": "H02",
            "ex_date": "2022-07-15",
            "subject": "Dividend - Rs Per Share",
            "reason": dividends["unparsed_subjects"][0]["reason"],
        }
    ]


def test_every_demerger_credit_is_listed_with_its_effect_on_excess_return(
    reported: Reported,
) -> None:
    database_url, _, _, report = reported
    (h04,) = _positions(database_url, "H04")

    (credit,) = report["stitched_curve"]["portfolio"]["demerger_credits"]

    cash = (D(h04.shares) / 10 * D(50)).quantize(D("0.01"))
    assert (credit["parent"], credit["child"], credit["day"]) == ("H04", "HFOUR", "2022-12-15")
    assert D(credit["cash"]) == cash
    assert D(credit["effect_on_excess"]) == (cash / STARTING_EQUITY).quantize(D("0.000001"))


def test_every_rights_issue_held_through_is_listed_with_its_effect(reported: Reported) -> None:
    database_url, _, _, report = reported
    (h03,) = _positions(database_url, "H03")

    (rights,) = report["stitched_curve"]["portfolio"]["rights_issues"]

    assert (rights["symbol"], rights["ex_date"], rights["shares"]) == (
        "H03",
        "2022-11-15",
        h03.shares,
    )
    fall = D(rights["open"]) - D(rights["previous_close"])
    assert fall == D("-1.00")
    assert D(rights["effect_on_excess"]) == (h03.shares * fall / STARTING_EQUITY).quantize(
        D("0.000001")
    )


def test_the_report_states_its_caveats_and_the_benchmarks_tax(reported: Reported) -> None:
    _, _, _, report = reported
    notes = report["notes"]

    assert "surcharge" in notes["surcharge"]
    assert "buffer of 60" in notes["buffer_scale"]
    assert notes["rebalances_counted"] == REBALANCES_COUNTED
    # lowvol_v1's Holdout run here is the family's first: independent.
    assert notes["not_independent"] is None
    for curve in ("stitched_curve", "holdout"):
        liquidation = report[curve]["liquidation"]
        assert liquidation["benchmark"]["taxed_as"] in ("short_term", "long_term")
        assert liquidation["benchmark"]["tax_rate"]
        assert set(liquidation["losses_dropped"]) == {"short_term", "long_term"}


def test_the_markdown_renders_the_additions_after_the_gate(reported: Reported) -> None:
    database_url, frozen, config, _ = reported
    markdown = (Path(str(config)).parent / "first" / "report.md").read_text()

    gate = markdown.index("## Backtest Gate")
    assert gate < markdown.index("Information ratio") < markdown.index("## Halt would have fired")
    for section in (
        "## Against the Benchmark",
        "## Factor fund",
        "## Rebalances: sector weights and cash",
        "## Dividends, Demerger Credits and rights issues",
        "## Notes",
    ):
        assert section in markdown
    assert "surcharge" in markdown


def test_two_reports_of_one_protocol_over_the_same_data_write_the_same_json(
    reported: Reported, tmp_path: Path
) -> None:
    database_url, frozen, config, _ = reported
    first = (Path(str(config)).parent / "first" / "report.json").read_bytes()

    second = _report(database_url, config, frozen.hash, tmp_path / "second")

    assert second.read_bytes() == first
