"""``lowvol_v1`` end to end on synthetic data: its Protocol frozen from ``[lowvol]`` by
``tradeline protocol freeze --strategy lowvol_v1``, its one-cell Walk-Forward run by
``tradeline backtest --protocol P``, and the Stitched Curve's account read back through
``tradeline.queries`` and the Walk-Forward report's JSON.

The fixture is ``synthetic_eod.lowvol_market``: seventy flat stocks whose volatility is
their swing. The Protocol's Out-of-Sample Windows are the months 2022-04 to 2022-10, so
the account's first trading day is 2022-04-01 and its Initial Buy is decided on
2022-03-31's close; its one Rebalance is decided on 2022-09-30, sells on 2022-10-03 and
buys on 2022-10-04 (2022-10-05 is a holiday, after it). Its last day, 2022-10-31, ends
with a Boundary Exit.

- Initial Buy (as of 2022-03-31): N01 to N33 are ineligible (a missing day, N33 a
  suspension), so H01 to H30 rank 1 to 30 and are bought, and O01, tied with H30 and after
  it by Symbol, ranks 31st and is not.
- Rebalance (as of 2022-09-30): N01 to N33 rank 1 to 33; H15 is ineligible (a missing
  day) and H20 left the Universe in 2022-08; H29 ranks 60th and stays, H30 61st and
  leaves. Three leave, so the three best-ranked names not held, N01 to N03, enter.
- H20 gapped Rs 4,000 up, so its sale leaves more cash than three equal shares of 15
  percent of Equity can take: each entrant is capped at 15 percent, the rest stays in cash.
- Relative Drawdown Halt: the recorded niftyindices.com index history is loaded before the
  freeze (threshold 30 percent), and the Benchmark's NAV (``lowvol_benchmark_navs``) is Rs
  100 until it rises Rs 2 a trading day from 2022-08-02, so the Halt would fire through
  September and October. A second account is run under Halt from the Rebalance's day.
"""

import contextlib
import io
import json
import uuid
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from limits import LOWVOL
from synthetic_eod import (
    LOWVOL_BENCHMARK_RISE,
    LOWVOL_INITIAL,
    LOWVOL_LAYOUT,
    LOWVOL_RANGE,
    LOWVOL_REBALANCE,
    REPO_ROOT,
    compute_universe_months,
    config_text,
    load_benchmark,
    load_demerger_table,
    load_index_history,
    load_market,
    lowvol_benchmark_navs,
    lowvol_market,
    pass_holdout_checks,
    repo_calendar,
    write_benchmark_fixtures,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.engine import SeriesCache, backtest_span, run
from tradeline.report import read_account, relative_drawdown_days_of
from tradeline.strategy.lowvol import LowvolV1
from tradeline.tick_sizes import load_tick_sizes

D = Decimal
CALENDAR = repo_calendar()
FIRST_DAY = date(2022, 4, 1)
SALES = date(2022, 10, 3)
BUYS = date(2022, 10, 4)
LAST_DAY = date(2022, 10, 31)
HELD_AT_START = [f"H{j:02}" for j in range(1, 31)]


def _config(directory: Path) -> Path:
    layout = LOWVOL_LAYOUT
    config = directory / "tradeline.toml"
    config.write_text(
        config_text(lowvol_market()) + "[lowvol.protocol]\n"
        f'holdout_start = "{layout.holdout_start:%Y-%m}"\n'
        f"holdout_months = {layout.holdout_months}\n"
        f"windows = {layout.windows}\n"
        f"out_of_sample_months = {layout.out_of_sample_months}\n"
        f"in_sample_months = {layout.in_sample_months}\n"
        f'[tax]\nschedule = "{REPO_ROOT / "tax_rates.csv"}"\n'
        f'[benchmark]\nfunds = "{REPO_ROOT / "benchmark_funds.csv"}"\n'
    )
    return config


def _cli(database_url: str, config: Path, *argv: str) -> None:
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        assert main(list(argv)) == 0


@pytest.fixture(scope="module")
def stitched(
    database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID]:
    """The market loaded, ``lowvol_v1``'s Protocol frozen through the CLI and its
    Walk-Forward run: the database, the Protocol, the report's JSON and the Stitched
    Curve's backtest id."""
    market = lowvol_market()
    directory = tmp_path_factory.mktemp("lowvol")
    fixtures = write_fixtures(market, directory / "fixtures", CALENDAR)
    navs = write_benchmark_fixtures(
        directory / "amfi", lowvol_benchmark_navs(CALENDAR), end=LOWVOL_RANGE[1]
    )
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        compute_universe_months(conn, market, *LOWVOL_RANGE)
        load_benchmark(conn, navs, end=LOWVOL_RANGE[1])
        load_demerger_table(conn)
        load_index_history(conn)
    config = _config(directory)
    _cli(database_url, config, "protocol", "freeze", "--strategy", "lowvol_v1")
    with app_connection(database_url) as conn:
        (found,) = queries.protocols(conn)
    out = directory / "walk_forward.json"
    _cli(database_url, config, "backtest", "--protocol", found.hash, "--out", str(out))
    with app_connection(database_url) as conn:
        walked = queries.walk_forward(conn, found.hash)
    assert walked is not None
    return database_url, found, json.loads(out.read_text()), walked.backtest_id


def _account(database_url: str, backtest_id: uuid.UUID) -> dict[str, Any]:
    tags: dict[str, Any] = {"mode": "backtest", "backtest_id": backtest_id}
    with app_connection(database_url) as conn:
        return {
            "runs": queries.runs(conn, **tags),
            "positions": queries.positions(conn, **tags),
            "fills": queries.fills(conn, **tags),
        }


def _opened(account: dict[str, Any], day: date) -> list[str]:
    return sorted(p.symbol for p in account["positions"] if p.opened_on == day)


def test_the_protocol_freezes_lowvols_limit_set_and_one_cell(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    _, frozen, _, _ = stitched
    terms = frozen.protocol

    assert (terms.family, terms.name) == ("lowvol", "lowvol_v1")
    assert terms.limits == LOWVOL
    assert terms.grid == {}
    assert terms.windows[0].out_of_sample.first == FIRST_DAY


def test_the_initial_buy_buys_the_30_least_volatile_eligible_on_the_first_day(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)

    # N01 to N33 miss a day (N33 a suspension) in the 253 trading days to 2022-03-31; O01
    # ties H30 and ranks after it by Symbol.
    assert _opened(account, FIRST_DAY) == HELD_AT_START
    (decided,) = [r for r in account["runs"] if r.as_of < FIRST_DAY]
    assert (decided.as_of, decided.portfolio_step) == (LOWVOL_INITIAL, "initial_buy")
    assert {p.run_id for p in account["positions"] if p.opened_on == FIRST_DAY} == {decided.id}


def test_the_initial_buy_is_not_a_rebalance(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)

    # 2022-03-31 is a last trading day of March, but its Run is the Initial Buy's.
    rebalances = [r.as_of for r in account["runs"] if r.portfolio_step == "rebalance"]
    assert rebalances == [LOWVOL_REBALANCE]


def test_the_initial_buy_splits_the_starting_cash_equally_at_the_limit_over_the_close(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)
    (decided,) = [r for r in account["runs"] if r.as_of == LOWVOL_INITIAL]
    share = D(500000) / 30
    with psycopg.connect(database_url) as conn:
        passes = queries.passes(conn, decided.id)
        closes = {
            p.symbol: queries.end_of_day(
                conn, p.symbol, start=LOWVOL_INITIAL, end=LOWVOL_INITIAL, as_of=LOWVOL_INITIAL
            )[0].close
            for p in passes
        }

    assert len(passes) == 30
    for granted in passes:
        close = closes[granted.symbol]
        assert close is not None
        # The limit is the close plus the 1 percent band, rounded up to the 5 paisa tick.
        ticks = (close * D("1.01") / D("0.05")).to_integral_value(rounding="ROUND_CEILING")
        assert granted.entry == ticks * D("0.05")
        assert (granted.stop, granted.target, granted.time_stop_days) == (None, None, None)
        # The thirtieth of the cash, less the buy's charges (well under 1 percent).
        assert granted.shares * granted.entry <= share
        assert (granted.shares + 1) * granted.entry > share * D("0.99")


def test_between_rebalances_no_holding_is_traded(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)

    assert sorted({f.day for f in account["fills"]}) == [FIRST_DAY, SALES, BUYS, LAST_DAY]


def test_the_account_never_spends_more_cash_than_it_holds(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)

    assert min(r.cash for r in account["runs"]) >= 0


def test_the_rebalance_sells_its_leavers_at_the_next_open(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)

    sold = {p.symbol: p for p in account["positions"] if p.exit_reason == "rebalance"}
    # H15 became ineligible, H20 left the Universe, H30 ranks 61st; H29, 60th, stays.
    assert sorted(sold) == ["H15", "H20", "H30"]
    assert {p.as_of for p in sold.values()} == {SALES}
    with psycopg.connect(database_url) as conn:
        for symbol, position in sold.items():
            (bar,) = queries.end_of_day(conn, symbol, start=SALES, end=SALES, as_of=SALES)
            assert position.exit_price == bar.open
    boundary = sorted(p.symbol for p in account["positions"] if p.exit_reason == "boundary")
    assert "H29" in boundary
    assert "O01" not in boundary


def test_the_rebalance_buys_the_best_ranked_not_held_the_day_after_its_sales(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)

    assert _opened(account, BUYS) == ["N01", "N02", "N03"]
    steps = {r.as_of: r.portfolio_step for r in account["runs"]}
    assert (steps[LOWVOL_REBALANCE], steps[SALES], steps[BUYS]) == (
        "rebalance",
        "rebalance_buys",
        None,
    )


def test_each_entrant_is_capped_at_15_percent_of_equity_and_the_rest_stays_in_cash(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, _, backtest_id = stitched
    account = _account(database_url, backtest_id)
    by_day = {r.as_of: r for r in account["runs"]}
    sized = by_day[SALES]
    with psycopg.connect(database_url) as conn:
        passes = queries.passes(conn, sized.id)
    assert sized.equity is not None and sized.cash is not None
    bought = sum(
        (f.price * f.shares + f.charges.total for f in account["fills"] if f.day == BUYS),
        D(0),
    )

    assert sorted(p.symbol for p in passes) == ["N01", "N02", "N03"]
    for granted in passes:
        # H20's sale left more cash than three shares of 15 percent of Equity can take.
        assert granted.facts["allowed_requested"] > granted.facts["allowed_max_position"]
        assert granted.shares == granted.facts["allowed_max_position"]
        assert granted.shares * granted.entry <= sized.equity * D("0.15")
    left = by_day[BUYS].cash
    assert left == sized.cash - bought
    assert left > sized.cash - 3 * sized.equity * D("0.15")


def test_walk_forward_selects_nothing_and_the_stitched_curve_is_one_backtest(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    _, _, report, _ = stitched

    windows = report["windows"]
    assert len(windows) == 7
    for window in windows:
        assert window["selection"] is None
        assert window["cells"] == 1
        assert "one cell" in window["no_selection"]
    curve = report["stitched_curve"]
    # The Initial Buy's decision is the day before; the curve starts on the first day.
    assert (curve["from"], curve["to"]) == (FIRST_DAY.isoformat(), LAST_DAY.isoformat())
    assert curve["trading_days"] == len(list(CALENDAR.trading_days(FIRST_DAY, LAST_DAY)))


def test_the_protocol_freezes_the_relative_drawdown_halt_from_the_recorded_index_history(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    _, frozen, _, _ = stitched
    halts = frozen.protocol.halts
    relative = halts.relative_drawdown

    # The daily-loss and drawdown Halts are off for lowvol (ADR 0020).
    assert (halts.daily_loss, halts.drawdown) == (None, None)
    assert relative is not None
    # Worked from the recorded closes (fixtures/index_history/README.md): the NIFTY100 Low
    # Volatility 30 TRI over the NIFTY 500 TRI peaked on 2006-08-18 and fell 25.29 percent
    # to 2008-01-14; rounded up to the next 5 points, 30 percent.
    assert relative.threshold == D("0.30")
    assert relative.worst == D("0.252946")
    assert (relative.peak, relative.trough) == (date(2006, 8, 18), date(2008, 1, 14))
    assert not relative.fallback
    assert "niftyindices.com" in relative.source
    assert "backfill" in relative.note
    assert [(h.index_name, h.first, h.last, h.days) for h in relative.history] == [
        ("NIFTY100 LOW VOLATILITY 30", date(2005, 4, 1), date(2019, 9, 30), 3595),
        ("NIFTY 500", date(2005, 4, 1), date(2019, 9, 30), 3595),
    ]
    assert frozen.protocol.benchmark is not None
    assert frozen.protocol.benchmark.funds.schemes == (147625,)


def test_the_report_lists_every_day_the_relative_drawdown_halt_would_have_fired_near_the_top(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, _, report, backtest_id = stitched
    navs = lowvol_benchmark_navs(CALENDAR)
    runs = [r for r in _account(database_url, backtest_id)["runs"] if r.as_of >= FIRST_DAY]
    # The Relative Drawdown as defined (ADR 0020): Equity at each close over the Benchmark's
    # NAV, its fall from that ratio's running peak.
    # Before tax: the tax debited on or before the day added back.
    with app_connection(database_url) as conn:
        debits = queries.tax_debits(conn, mode="backtest", backtest_id=backtest_id)
    expected, peak = {}, D(0)
    for r in runs:
        assert r.equity is not None
        paid = sum((t.tax for t in debits if t.day <= r.as_of), D(0))
        ratio = (r.equity + paid) / navs[r.as_of]
        peak = max(peak, ratio)
        if 1 - ratio / peak >= D("0.30"):
            expected[r.as_of.isoformat()] = str((1 - ratio / peak).quantize(D("0.000001")))

    assert list(report)[:3] == ["protocol", "strategy", "halt_would_have_fired"]
    fired = report["halt_would_have_fired"]
    assert fired["relative_drawdown_limit"] == "0.300000"
    assert fired["fallback"] is False
    listed = {d["day"]: d["relative_drawdown"] for d in fired["days"]}
    assert listed == expected
    # Flat until the Benchmark rises; by mid-September it has risen over 40 percent.
    assert min(listed) > LOWVOL_BENCHMARK_RISE.isoformat()
    late = [d.isoformat() for d in CALENDAR.trading_days(date(2022, 9, 15), LAST_DAY)]
    assert set(late) <= set(listed)


def test_a_tax_debit_alone_makes_no_day_the_relative_drawdown_halt_would_have_fired(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, frozen, _, backtest_id = stitched
    # The Benchmark is flat from March to July: an account over March and April whose
    # Equity is flat but for the year's debit on 2022-04-01, a fall of over 80 percent
    # after tax and none before it.
    days = list(CALENDAR.trading_days(date(2022, 3, 1), date(2022, 4, 29)))
    debit = queries.TaxDebit(
        day=FIRST_DAY,
        financial_year=2021,
        short_term_gain=D("2000000"),
        long_term_gain=D(0),
        dividends=D(0),
        tax=D("416000"),
        settled=False,
        dropped_short_term_loss=None,
        dropped_long_term_loss=None,
    )
    start = frozen.protocol.starting_equity
    with app_connection(database_url) as conn:
        account = replace(
            read_account(conn, backtest_id).through(FIRST_DAY - timedelta(days=1)),
            days=tuple(days),
            curve=(start, *(start - (debit.tax if d >= FIRST_DAY else 0) for d in days)),
            tax_debits=(debit,),
        )
        fired = relative_drawdown_days_of(conn, frozen.protocol, account)

    assert fired == []


def test_a_halted_rebalance_sells_its_leavers_and_places_no_buy(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
) -> None:
    database_url, frozen, _, _ = stitched
    terms = frozen.protocol
    tick_sizes = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
    backtest_id = uuid.uuid4()
    with app_connection(database_url) as conn:
        backtest_span(
            conn,
            LowvolV1(),
            frozen,
            start=FIRST_DAY,
            end=date(2022, 9, 29),
            calendar=CALENDAR,
            tick_sizes=tick_sizes,
            backtest_id=backtest_id,
            cache=SeriesCache(conn, start=FIRST_DAY, end=BUYS),
        )
        # Under Halt from the Rebalance's day: the engine's Runs as paper would make them.
        for day in (LOWVOL_REBALANCE, SALES, BUYS):
            run(
                conn,
                LowvolV1(),
                mode="backtest",
                backtest_id=backtest_id,
                as_of=day,
                calendar=CALENDAR,
                rule_version=terms.rule_version,
                limits=terms.limits,
                tick_sizes=tick_sizes,
                costs=terms.cost_model(),
                starting_equity=terms.starting_equity,
                band=terms.band,
                protocol=frozen.hash,
                halted=True,
            )
        orders = queries.orders(conn, mode="backtest", backtest_id=backtest_id)
    account = _account(database_url, backtest_id)

    sold = sorted(p.symbol for p in account["positions"] if p.exit_reason == "rebalance")
    assert sold == ["H15", "H20", "H30"]
    steps = {r.as_of: r.portfolio_step for r in account["runs"]}
    assert (steps[LOWVOL_REBALANCE], steps[SALES]) == ("rebalance", "rebalance_buys")
    # The Initial Buy's entries, and none after.
    assert {o.session for o in orders if o.purpose == "entry"} == {FIRST_DAY}
    assert _opened(account, BUYS) == []


def test_the_full_report_lists_the_relative_drawdown_halt_days_of_each_curve_after_the_gate(
    stitched: tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID],
    tmp_path: Path,
) -> None:
    database_url, frozen, walked, _ = stitched
    config = _config(tmp_path)
    out, markdown = tmp_path / "report.json", tmp_path / "report.md"
    with app_connection(database_url) as conn:
        pass_holdout_checks(conn, frozen.hash)
    _cli(database_url, config, "backtest", "holdout", "--protocol", frozen.hash)
    _cli(
        database_url,
        config,
        *("backtest", "report", "--protocol", frozen.hash),
        *("--out", str(out), "--markdown", str(markdown)),
    )
    report = json.loads(out.read_text())

    assert list(report)[:4] == ["protocol", "strategy", "gate", "halt_would_have_fired"]
    fired = report["halt_would_have_fired"]
    assert fired["relative_drawdown_limit"] == "0.300000"
    assert fired["stitched_curve"] == walked["halt_would_have_fired"]["days"]
    # The Holdout (2022-11) is an account of its own, its peak from its first day: the
    # Benchmark rises from about Rs 226 to Rs 268 in the month, a fall of the ratio of
    # under 20 percent with the swing.
    assert fired["holdout"] == []
    text = markdown.read_text()
    assert "Relative Drawdown Halt" in text
    assert text.index("## Halt would have fired") < text.index("## Windows")
