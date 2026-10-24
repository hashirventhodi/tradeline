"""A Run ends in Verdicts: the Risk Engine's Passes and Blocks on the synthetic market,
read back through the query layer.

The market is ``synthetic_eod.base_market``. On ``RUN_DAY`` (2023-06-15) its
Candidates, in Plan order, are ALPHA (entry 280.00, stop 274.00, target
292.00), GAMMA (560.00, 548.00, 584.00) and BETA (480.00, 474.00, 492.00), as
``test_run.py`` works out; each is already on the 5 paisa tick in force then.
Equity is Rs 5,00,000 with nothing held, under the default Limits. Worked by
hand:

- ALPHA: 5,000 at risk over 6.00 a share is 833 shares (Rs 2,33,240), over
  the 15 percent per Position (75,000): 267 shares, Rs 74,760.
- GAMMA: 5,000 / 12.00 is 416, reduced to 75,000 / 560: 133 shares, Rs 74,480.
- BETA: 5,000 / 6.00 is 833, reduced to 75,000 / 480: 156 shares, if its
  sector has room.

With no sector load, all three are Unclassified: ALPHA and GAMMA use 1,49,240
of its 1,50,000, so BETA gets 1 share (Rs 480), under the Rs 5,000 minimum,
and is Blocked by the sector Limit.

``liquid_market`` loads a sector for each, from a list NSE published after
``RUN_DAY`` (the earliest load, which a day before it reads): ALPHA and GAMMA
in Capital Goods, BETA in Chemicals. There BETA trades only 1,000 shares a day
(still fifth of six in the Universe): its 20-day average traded value is
475.25 (the average of its closes 470.50 to 480.00) x 1,000 = 4,75,250, and 1
percent of that, 4,752.50, buys 9 shares (Rs 4,320), under the minimum, so it
is Blocked by the Participation Cap.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from synthetic_eod import (
    REPO_ROOT,
    RUN_DAY,
    base_market,
    compute_universe_months,
    load_market,
    load_sectors_on,
    repo_calendar,
    repo_costs,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.engine import run
from tradeline.risk import Limits
from tradeline.strategy.pricing import PricedPlan
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes

D = Decimal
MARKET = base_market()
SECTOR_LOAD = date(2024, 6, 28)


@pytest.fixture(scope="module")
def loaded(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    fixtures = write_fixtures(MARKET, tmp_path_factory.mktemp("synthetic"), repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, RUN_DAY, RUN_DAY)
    return database_url


@pytest.fixture
def conn(loaded: str) -> Iterator[psycopg.Connection]:
    with app_connection(loaded) as conn:
        yield conn


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loaded: str) -> None:
    rule = MARKET.rule
    path = tmp_path / "tradeline.toml"
    path.write_text(
        f"[universe]\ntop_n = {rule.top_n}\nlookback_trading_days = {rule.lookback_trading_days}\n"
        f"min_listing_trading_days = {rule.min_listing_trading_days}\n"
        f'[integrity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[parity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[risk]\ntick_sizes = "{REPO_ROOT / "tick_sizes.csv"}"\n'
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(path))
    monkeypatch.setenv("DATABASE_URL", loaded)


def test_a_run_writes_buy_verdicts_with_passes_and_skip_verdicts_with_blocks(
    config: None, conn: psycopg.Connection
) -> None:
    assert main(["run", "--mode", "backtest", "--date", RUN_DAY.isoformat()]) == 0

    (ran,) = queries.runs(conn, as_of=RUN_DAY)
    verdicts = queries.verdicts(conn, ran.id)
    assert [(v.rank, v.symbol, v.verdict, v.reason) for v in verdicts] == [
        (1, "ALPHA", "BUY", "pass"),
        (2, "GAMMA", "BUY", "pass"),
        (3, "BETA", "SKIP", "max_sector"),
    ]

    passes = queries.passes(conn, ran.id)
    assert [
        (p.symbol, p.entry, p.stop, p.target, p.time_stop_days, p.shares, p.sector) for p in passes
    ] == [
        ("ALPHA", D("280.00"), D("274.00"), D("292.00"), 10, 267, "unclassified"),
        ("GAMMA", D("560.00"), D("548.00"), D("584.00"), 10, 133, "unclassified"),
    ]
    alpha = passes[0]
    assert alpha.plan_hash == (
        PricedPlan(1, "ALPHA", "ALPHA", D("280"), D("274"), D("292"), 10, 267).content_hash()
    )
    assert alpha.facts["equity"] == D("500000")
    assert alpha.facts["tick"] == D("0.05")
    # ALPHA's raw closes 270.50 to 280.00 average 275.25, on 2,00,000 shares a day.
    assert alpha.facts["average_traded_value"] == D("55050000")

    (block,) = queries.blocks(conn, ran.id)
    assert (block.rank, block.symbol, block.limit, block.shares, block.sector) == (
        3,
        "BETA",
        "max_sector",
        1,
        "unclassified",
    )


def test_a_participation_cap_block_and_the_earliest_sector_load_read_before_it(
    another_database_url: str, tmp_path: Path
) -> None:
    market = (
        MARKET.with_stock("ALPHA", sector="Capital Goods")
        .with_stock("GAMMA", sector="Capital Goods")
        .with_stock("BETA", sector="Chemicals", volume=1_000)
    )
    fixtures = write_fixtures(market, tmp_path, repo_calendar())
    with app_connection(another_database_url) as conn:
        load_market(conn, fixtures, market)
        load_sectors_on(conn, fixtures, SECTOR_LOAD)
        compute_universe_months(conn, market, RUN_DAY, RUN_DAY)
        result = run(
            conn,
            PullbackV1(),
            mode="backtest",
            as_of=RUN_DAY,
            calendar=repo_calendar(),
            rule_version=market.rule.version,
            backtest_id=uuid.uuid4(),
            limits=Limits(),
            tick_sizes=load_tick_sizes(REPO_ROOT / "tick_sizes.csv"),
            starting_equity=D("500000"),
            costs=repo_costs(),
        )
        verdicts = queries.verdicts(conn, result.run_id)
        passes = queries.passes(conn, result.run_id)
        (block,) = queries.blocks(conn, result.run_id)

    assert [(v.symbol, v.verdict, v.reason) for v in verdicts] == [
        ("ALPHA", "BUY", "pass"),
        ("GAMMA", "BUY", "pass"),
        ("BETA", "SKIP", "participation_cap"),
    ]
    assert [(p.symbol, p.shares, p.sector) for p in passes] == [
        ("ALPHA", 267, "Capital Goods"),
        ("GAMMA", 133, "Capital Goods"),
    ]
    assert (block.symbol, block.shares, block.sector) == ("BETA", 9, "Chemicals")
    assert block.facts["average_traded_value"] == D("475250")
