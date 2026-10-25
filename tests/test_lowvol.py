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
"""

import contextlib
import io
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from limits import LOWVOL
from synthetic_eod import (
    LOWVOL_INITIAL,
    LOWVOL_LAYOUT,
    LOWVOL_RANGE,
    LOWVOL_REBALANCE,
    compute_universe_months,
    config_text,
    load_market,
    lowvol_market,
    repo_calendar,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection

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
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        compute_universe_months(conn, market, *LOWVOL_RANGE)
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
