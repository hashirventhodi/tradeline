"""A Position held across a split and a bonus: the engine restates it on each Ex-Date.

The market is ``synthetic_eod.two_year_market`` with ASTER split Rs 10 to Rs 5
on 2022-08-10 and given a 1:1 bonus on 2022-08-17, both while the Position
from its 2022-08-03 pullback is held; backtested from 2022-07-01 to 2022-09-30
with ``pullback_v1`` at k = 2, r = 2 and n = 30, so the Position is held until
its target. On its path ASTER's close on the Market's i-th trading day is 400
+ i, its open the close before and its high and low 2 outside them; NSE
publishes the path times 1/2 from the split and times 1/4 from the bonus.
Worked by hand:

- The Plan on 2022-08-03 (close 673): stop 661, target 697, 111 shares; filled
  on 2022-08-04 at 673.
- 2022-08-10, the split's Ex-Date: shares 222, entry 336.50, stop 330.50,
  target 348.50, no cash in lieu. The target resting for the day (697, 111
  shares) is cancelled as NSE purges it, and placed again at 348.50 for 222.
  ASTER's low that day, 337.50 (path 675), is above the restated stop; read
  against the old stop of 661 it would have been a stop hit.
- 2022-08-17, the bonus's Ex-Date: shares 444, entry 168.25, stop 165.25,
  target 174.25.
- Equity marks 111 path shares at the close on every day, so it rises Rs 111 a
  day from one trading day to the next, the Ex-Dates included: the actions move
  it by nothing.
- The path's high first passes 697 on 2022-09-08 (close 696, high 698), the
  restated target 174.25 (high 174.50): the target fills at 174.25 for 444
  shares, Rs 77,367, the 111 shares' 697 each.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import psycopg
import pytest

from synthetic_eod import (
    Bonus,
    Split,
    compute_universe_months,
    load_market,
    repo_calendar,
    repo_costs,
    two_year_market,
    write_fixtures,
)
from tradeline import queries
from tradeline.db import app_connection
from tradeline.engine import STARTING_EQUITY, backtest
from tradeline.risk import Limits
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes

D = Decimal
SPLIT, BONUS = date(2022, 8, 10), date(2022, 8, 17)
MARKET = two_year_market().with_events(
    "ASTER", Split(SPLIT, from_face=10, to_face=5), Bonus(BONUS, new=1, held=1)
)
START, END = date(2022, 7, 1), date(2022, 9, 30)


@pytest.fixture(scope="module")
def backtested(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    fixtures = write_fixtures(MARKET, tmp_path_factory.mktemp("actions"), repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, START, END)
        backtest(
            conn,
            PullbackV1(n=30),
            start=START,
            end=END,
            calendar=repo_calendar(),
            rule_version=MARKET.rule.version,
            limits=Limits(),
            tick_sizes=load_tick_sizes(Path(__file__).parents[1] / "tick_sizes.csv"),
            costs=repo_costs(),
            starting_equity=STARTING_EQUITY,
        )
    return database_url


@pytest.fixture
def conn(backtested: str) -> Iterator[psycopg.Connection]:
    with app_connection(backtested) as conn:
        yield conn


def aster_events(conn: psycopg.Connection) -> list[queries.LedgerPositionEvent]:
    (run,) = {r.backtest_id for r in queries.runs(conn, mode="backtest")}
    events = queries.position_events(conn, mode="backtest", backtest_id=run)
    (first,) = {e.position_id for e in events if e.symbol == "ASTER" and e.day < SPLIT}
    return [e for e in events if e.position_id == first]


def test_a_held_position_is_restated_on_each_ex_date_and_exits_at_the_restated_target(
    conn: psycopg.Connection,
) -> None:
    events = aster_events(conn)

    assert [
        (e.event, e.day, e.shares, e.entry, e.stop, e.target, e.factor, e.cash_in_lieu)
        + (e.exit_reason, e.exit_price)
        for e in events
    ] == [
        ("opened", date(2022, 8, 4), 111, D(673), D(661), D(697), None, None, None, None),
        ("restated", SPLIT, 222, D("336.50"), D("330.50"), D("348.50"))
        + (Fraction(1, 2), D(0), None, None),
        ("restated", BONUS, 444, D("168.25"), D("165.25"), D("174.25"))
        + (Fraction(1, 2), D(0), None, None),
        ("closed", date(2022, 9, 8), 444, D("168.25"), D("165.25"), D("174.25"))
        + (None, None, "target", D("174.25")),
    ]


def test_the_ex_date_purges_the_resting_target_and_places_it_again_restated(
    conn: psycopg.Connection,
) -> None:
    (run,) = {r.backtest_id for r in queries.runs(conn, mode="backtest")}
    targets = [
        o
        for o in queries.orders(conn, mode="backtest", backtest_id=run)
        if o.symbol == "ASTER" and o.purpose == "target" and o.session == SPLIT
    ]

    assert [(o.limit_price, o.shares, o.status, o.cancel_reason) for o in targets] == [
        (D(697), 111, "cancelled", "corporate_action"),
        (D("348.50"), 222, "cancelled", "replaced"),
    ]


def test_equity_is_unchanged_by_a_split_or_a_bonus(conn: psycopg.Connection) -> None:
    equity = {r.as_of: r.equity or D(0) for r in queries.runs(conn, mode="backtest")}
    # Consecutive trading days (2022-08-09 and 2022-08-15 are holidays).
    pairs = [
        (date(2022, 8, 8), SPLIT),
        (SPLIT, date(2022, 8, 11)),
        (date(2022, 8, 16), BONUS),
        (BONUS, date(2022, 8, 18)),
    ]

    assert [equity[b] - equity[a] for a, b in pairs] == [D(111)] * 4
