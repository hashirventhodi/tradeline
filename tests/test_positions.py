"""Fills and Positions over many days: ``tradeline backtest --from --to`` over two years of the
synthetic market, read back through the query layer.

The market is ``synthetic_eod.two_year_market``, backtested from 2022-07-01 to
2024-06-28 with ``pullback_v1`` at k = 2, r = 2, n = 10, Equity Rs 5,00,000 and
the default Limits. Each pullback's Plan enters at its close C, stops at C - 12
and targets C + 24; at 15 percent of Equity it buys floor(75,000 / C) shares.
Its entry fills the next day at that day's open, which is C, unless the open is
above it. An exit's limit is the close less 5 percent, rounded down to the 5
paisa tick. A Stock's close on the Market's i-th trading day is its start plus
i, until an event moves it. Worked by hand:

- ASTER, 2022-08-03 (day 273): C = 673, 111 shares, stop 661, target 697. Filled
  on 2022-08-04; the lows (from 670) never reach the stop and the highs reach
  697 only on day 296. The 10th trading day after the Fill is 2022-08-22 (the
  Trading Calendar is closed on 08-09 and 08-15), whose close is 684: the time
  stop fires, its exit is limited at 649.80 and fills at the next open, 684.
- BIRCH, 2022-11-01 (day 332): C = 632, 118 shares, stop 620, target 656. Filled
  on 2022-11-02, from which it rises 2.75 a day: the high on the k-th day of the
  Position is 632 + 2.75k + 2, equal to 656 on 2022-11-14 (no fill) and 658.75
  on 2022-11-15, whose open is 654: the target fills at 656.
- CEDAR, 2023-01-02 (day 375): C = 575, 130 shares, stop 563, target 599. Filled
  on 2023-01-03 (close 576); 2023-01-04 opens 30 higher, at 606, above the
  target: it fills at the open.
- ASTER, 2023-03-01 (day 416, its close now 400 + i): C = 816, 91 shares, stop
  804, target 840. Filled on 2023-03-02 (close 817). On 2023-03-03 it opens at
  817, closes at 847, high 849 and low 803: both the stop and the target are
  reached, so the target fills nothing and the stop fires; that day's Run sees
  ASTER as a Candidate again and skips it as held. The exit, limited at 804.65,
  fills at the next open, 847.
- BIRCH, 2023-05-02 (day 454, its close now 315.75 + i): C = 769.75. 2023-05-03
  opens 5 higher, at 774.75: the entry is cancelled.
- ASTER, 2024-01-03 (day 623, its close now 429 + i): C = 1052, 71 shares, stop
  1040, target 1076. Filled on 2024-01-04 (close 1053). 2024-01-05 opens 15
  lower, at 1038, closes at 1039 with its low at 1036: the stop fires, limited at
  987.05. 2024-01-08 opens 60 lower, at 979, under the limit: the exit is
  cancelled, the Position stays open, and that day's Run sends it again at its
  close 978 less the band, 929.10. 2024-01-09 opens at 978: filled.
- BIRCH, 2024-03-12 (day 671, its close now 320.75 + i): C = 991.75, 75 shares,
  stop 979.75, target 1015.75. Filled on 2024-03-13 (close 992.75). 2024-03-14
  is a Circuit Lock at 972.75, under the stop: nothing fills, the stop fires,
  limited at 924.10. 2024-03-15 is a Circuit Lock at 971.75, above that limit:
  nothing fills, and the exit is sent again at 923.15. 2024-03-18 opens at
  971.75: filled.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from synthetic_eod import (
    REPO_ROOT,
    TWO_YEARS,
    compute_universe_months,
    load_market,
    repo_calendar,
    two_year_market,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection

D = Decimal
MARKET = two_year_market()


@pytest.fixture(scope="module")
def backtested(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    directory = tmp_path_factory.mktemp("two_years")
    fixtures = write_fixtures(MARKET, directory / "fixtures", repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, *TWO_YEARS)
    rule = MARKET.rule
    config = directory / "tradeline.toml"
    config.write_text(
        f"[universe]\ntop_n = {rule.top_n}\nlookback_trading_days = {rule.lookback_trading_days}\n"
        f"min_listing_trading_days = {rule.min_listing_trading_days}\n"
        f"min_presence_percent = {rule.min_presence_percent}\n"
        f'[integrity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[parity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[risk]\ntick_sizes = "{REPO_ROOT / "tick_sizes.csv"}"\n'
    )
    start, end = TWO_YEARS
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        assert main(["backtest", "--from", start.isoformat(), "--to", end.isoformat()]) == 0
    return database_url


@pytest.fixture
def conn(backtested: str) -> Iterator[psycopg.Connection]:
    with app_connection(backtested) as conn:
        yield conn


def backtest_id(conn: psycopg.Connection) -> uuid.UUID | None:
    """The one backtest in the database."""
    (found,) = {r.backtest_id for r in queries.runs(conn, mode="backtest")}
    return found


def orders(conn: psycopg.Connection, symbol: str, *purposes: str) -> list[queries.LedgerOrder]:
    return [
        o
        for o in queries.orders(conn, mode="backtest", backtest_id=backtest_id(conn))
        if o.symbol == symbol and o.purpose in purposes
    ]


def test_a_two_year_backtest_closes_each_filled_entry_with_its_exits_reason(
    conn: psycopg.Connection,
) -> None:
    positions = queries.positions(conn, mode="backtest", backtest_id=backtest_id(conn))

    assert [
        (p.symbol, p.opened_on, p.entry, p.shares, p.stop, p.target, p.closed_on, p.exit_price)
        + (p.exit_reason,)
        for p in positions
    ] == [
        ("ASTER", date(2022, 8, 4), D(673), 111, D(661), D(697), date(2022, 8, 23), D(684))
        + ("time",),
        ("BIRCH", date(2022, 11, 2), D(632), 118, D(620), D(656), date(2022, 11, 15), D(656))
        + ("target",),
        ("CEDAR", date(2023, 1, 3), D(575), 130, D(563), D(599), date(2023, 1, 4), D(606))
        + ("target",),
        ("ASTER", date(2023, 3, 2), D(816), 91, D(804), D(840), date(2023, 3, 6), D(847))
        + ("stop",),
        ("ASTER", date(2024, 1, 4), D(1052), 71, D(1040), D(1076), date(2024, 1, 9), D(978))
        + ("stop",),
        ("BIRCH", date(2024, 3, 13), D("991.75"), 75, D("979.75"), D("1015.75"))
        + (date(2024, 3, 18), D("971.75"), "stop"),
    ]
    assert (
        queries.positions(conn, open_only=True, mode="backtest", backtest_id=backtest_id(conn))
        == []
    )

    fills = queries.fills(conn, mode="backtest", backtest_id=backtest_id(conn))
    assert len(fills) == 12
    assert [(f.day, f.symbol, f.side, f.price, f.shares) for f in fills[:2]] == [
        (date(2022, 8, 4), "ASTER", "buy", D(673), 111),
        (date(2022, 8, 23), "ASTER", "sell", D(684), 111),
    ]


def test_an_entry_fills_at_an_open_at_its_limit_and_is_cancelled_by_an_open_above_it(
    conn: psycopg.Connection,
) -> None:
    entries = orders(conn, "BIRCH", "entry")

    assert [
        (o.session, o.limit_price, o.shares, o.status, o.cancel_reason, o.fill_price)
        for o in entries
    ] == [
        (date(2022, 11, 2), D(632), 118, "filled", None, D(632)),
        (date(2023, 5, 3), D("769.75"), 97, "cancelled", "open_above_limit", None),
        (date(2024, 3, 13), D("991.75"), 75, "filled", None, D("991.75")),
    ]


def test_a_target_fills_at_the_target_once_the_high_passes_it_and_at_an_open_above_it(
    conn: psycopg.Connection,
) -> None:
    birch = [o for o in orders(conn, "BIRCH", "target") if o.session <= date(2022, 11, 15)]
    # Placed with the Fill on 11-02, then re-placed by each Run for the next session.
    assert [o.session for o in birch] == [
        date(2022, 11, 2),
        date(2022, 11, 3),
        date(2022, 11, 4),
        date(2022, 11, 7),
        date(2022, 11, 9),
        date(2022, 11, 10),
        date(2022, 11, 11),
        date(2022, 11, 14),
        date(2022, 11, 15),
    ]
    assert {o.limit_price for o in birch} == {D(656)}
    assert [(o.status, o.cancel_reason) for o in birch[:-1]] == [("cancelled", "replaced")] * 8
    assert (birch[-1].status, birch[-1].fill_price) == ("filled", D(656))

    (cedar,) = [o for o in orders(conn, "CEDAR", "target") if o.status == "filled"]
    assert (cedar.session, cedar.limit_price, cedar.fill_price) == (
        date(2023, 1, 4),
        D(599),
        D(606),
    )


def test_a_bar_reaching_both_stop_and_target_is_a_stop_whose_sell_follows_the_targets_cancel(
    conn: psycopg.Connection,
) -> None:
    march = [o for o in orders(conn, "ASTER", "target", "stop") if o.session.year == 2023]
    target, stop = march[-2:]

    assert (target.session, target.status, target.cancel_reason) == (
        date(2023, 3, 3),
        "cancelled",
        "stop",
    )
    assert (stop.session, stop.limit_price, stop.status, stop.fill_price) == (
        date(2023, 3, 6),
        D("804.65"),
        "filled",
        D(847),
    )
    assert target.cancelled is not None
    assert target.cancelled < stop.placed

    (ran,) = queries.runs(conn, as_of=date(2023, 3, 3))
    assert [(v.symbol, v.verdict, v.reason) for v in queries.verdicts(conn, ran.id)] == [
        ("ASTER", "SKIP", "held")
    ]


def test_no_position_ever_has_two_open_sell_orders(conn: psycopg.Connection) -> None:
    events: list[tuple[tuple[date, int], queries.LedgerOrder, int]] = []
    for order in queries.orders(conn, mode="backtest", backtest_id=backtest_id(conn)):
        if order.side == "sell":
            events.append((order.placed, order, 1))
            ended = order.cancelled or order.filled
            assert ended is not None
            events.append((ended, order, -1))
    open_sells: dict[object, int] = defaultdict(int)
    most = 0
    for _, order, change in sorted(events, key=lambda e: e[0]):
        open_sells[order.position_id] += change
        most = max(most, open_sells[order.position_id])

    assert most == 1


def test_an_open_below_the_band_leaves_the_position_open_and_the_exit_is_sent_again(
    conn: psycopg.Connection,
) -> None:
    stops = [o for o in orders(conn, "ASTER", "stop") if o.session.year == 2024]

    assert [
        (o.placed[0], o.session, o.limit_price, o.status, o.cancel_reason, o.fill_price)
        for o in stops
    ] == [
        (date(2024, 1, 5), date(2024, 1, 8), D("987.05"), "cancelled", "open_below_limit", None),
        (date(2024, 1, 8), date(2024, 1, 9), D("929.10"), "filled", None, D(978)),
    ]


def test_a_circuit_lock_day_fills_nothing(conn: psycopg.Connection) -> None:
    march = [o for o in orders(conn, "BIRCH", "target", "stop") if o.session.year == 2024]

    assert [
        (o.session, o.purpose, o.limit_price, o.status, o.cancel_reason, o.fill_price)
        for o in march[-3:]
    ] == [
        (date(2024, 3, 14), "target", D("1015.75"), "cancelled", "stop", None),
        (date(2024, 3, 15), "stop", D("924.10"), "cancelled", "circuit_lock", None),
        (date(2024, 3, 18), "stop", D("923.15"), "filled", None, D("971.75")),
    ]
    locked = {date(2024, 3, 14), date(2024, 3, 15)}
    fills = queries.fills(conn, mode="backtest", backtest_id=backtest_id(conn))
    assert [f for f in fills if f.day in locked] == []


def test_the_time_stop_fires_n_trading_days_after_the_fill_on_the_trading_calendar(
    conn: psycopg.Connection,
) -> None:
    (time_exit,) = orders(conn, "ASTER", "time")

    assert (
        time_exit.placed[0],
        time_exit.session,
        time_exit.limit_price,
        time_exit.fill_price,
    ) == (date(2022, 8, 22), date(2022, 8, 23), D("649.80"), D(684))
