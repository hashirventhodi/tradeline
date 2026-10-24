"""``run --mode backtest``: one Run of ``pullback_v1`` over the synthetic market, read back
through the query layer.

The market is ``synthetic_eod.base_market``: on ``RUN_DAY`` (2023-06-15, the
360th trading day of the files) ALPHA, GAMMA, BETA, EPSILON and OMEGA dip below
their 20-day average and close above it; DELTA does not dip. Worked from the
description (``pullback_v1`` at k = 2, r = 2, n = 10):

- ALPHA's path is GAMMA's (560.00 = 200 + 360 x 1.00 on the day, rising 1.00 a
  day, half range 2.00, a dip of 17.00), halved by its Rs 10 to Rs 5 split in
  2022: it closes at 280.00, rising 0.50 a day. SMA20 = 280 - 9.5 x 0.50 = 275.25 and its low is
  271.50. SMA200 = 280 - 99.5 x 0.50 = 230.25, 20 days earlier 220.25. The
  126-day return is 280 / 217 - 1. ATR(14): thirteen bars of true range 2.50
  and the dip's 9.50 make 42 / 14 = 3.00, so the stop is 280 - 6 = 274.00 and
  the target 280 + 12 = 292.00.
- GAMMA is ALPHA unsplit: close 560.00, the same 126-day return, ATR 6.00,
  stop 548.00, target 584.00. The tie is broken by Symbol, so ALPHA ranks first.
- BETA closes at 480.00 (its 1:1 bonus is ex-dated after ``RUN_DAY``, so not
  applied), 126-day return 480 / 417 - 1, ATR 3.00, stop 474.00, target
  492.00.
- EPSILON falls, so its close is below its SMA200. OMEGA trades too little for
  the five-member Universe.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from synthetic_eod import (
    EXIT_BAND,
    REPO_ROOT,
    RUN_DAY,
    STARTING_EQUITY,
    Gap,
    Split,
    base_market,
    compute_universe_months,
    freeze_protocol,
    load_market,
    repo_calendar,
    repo_costs,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.engine import StoredMarketData, run
from tradeline.risk import Limits
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes

MARKET = base_market()
NEXT_DAY = date(2023, 6, 16)
RISK: dict[str, Any] = {
    "limits": Limits(),
    "tick_sizes": load_tick_sizes(REPO_ROOT / "tick_sizes.csv"),
    "starting_equity": STARTING_EQUITY,
    "band": EXIT_BAND,
    "costs": repo_costs(),
}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_fixtures(MARKET, tmp_path_factory.mktemp("synthetic"), repo_calendar())


@pytest.fixture(scope="module")
def loaded(database_url: str, fixtures: Path) -> str:
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, RUN_DAY, RUN_DAY)
    return database_url


@pytest.fixture(scope="module")
def protocol(loaded: str) -> str:
    """A Protocol whose Holdout is ``RUN_DAY``'s month, for the CLI's one-day backtest."""
    with app_connection(loaded) as conn:
        return freeze_protocol(conn, MARKET, date(2023, 4, 1), RUN_DAY).hash


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
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(path))
    monkeypatch.setenv("DATABASE_URL", loaded)


def test_run_in_backtest_mode_writes_one_run_with_its_ranked_candidates_and_plans(
    config: None, conn: psycopg.Connection, protocol: str
) -> None:
    day = RUN_DAY.isoformat()
    assert main(["run", "--mode", "backtest", "--date", day, "--protocol", protocol]) == 0

    (run,) = queries.runs(conn, as_of=RUN_DAY)
    assert run.mode == "backtest"
    assert run.backtest_id is not None
    assert run.strategy.name == "pullback_v1"
    assert run.strategy.family == "pullback"
    assert run.strategy.ranking == "126-trading-day return descending, ties by Symbol ascending"
    assert run.universe_rule_version == MARKET.rule.version
    assert (
        queries.universe_month_mode(
            conn, run.universe_month, rule_version=run.universe_rule_version
        )
        == "recorded"
    )

    candidates = queries.candidates(conn, run.id)
    assert [(c.rank, c.symbol) for c in candidates] == [(1, "ALPHA"), (2, "GAMMA"), (3, "BETA")]
    assert [c.score for c in candidates] == [
        Decimal(280) / Decimal(217) - 1,
        Decimal(560) / Decimal(434) - 1,
        Decimal(480) / Decimal(417) - 1,
    ]
    alpha = candidates[0]
    assert alpha.facts["close"] == Decimal("280.00")
    assert alpha.facts["sma20"] == Decimal("275.25")
    assert alpha.facts["sma200"] == Decimal("230.25")
    assert alpha.facts["sma200_20_days_earlier"] == Decimal("220.25")

    plans = queries.plans(conn, run.id)
    assert [(p.rank, p.symbol, p.entry, p.stop, p.target, p.time_stop_days) for p in plans] == [
        (1, "ALPHA", Decimal("280.00"), Decimal("274.00"), Decimal("292.00"), 10),
        (2, "GAMMA", Decimal("560.00"), Decimal("548.00"), Decimal("584.00"), 10),
        (3, "BETA", Decimal("480.00"), Decimal("474.00"), Decimal("492.00"), 10),
    ]


def backtest_run(conn: psycopg.Connection) -> tuple[list[tuple[object, ...]], ...]:
    """One Run as of ``RUN_DAY``: its Candidates and Plans, without the Run's id."""
    result = run(
        conn,
        PullbackV1(),
        mode="backtest",
        as_of=RUN_DAY,
        calendar=repo_calendar(),
        rule_version=MARKET.rule.version,
        backtest_id=uuid.uuid4(),
        **RISK,
    )
    candidates = queries.candidates(conn, result.run_id)
    plans = queries.plans(conn, result.run_id)
    return (
        [(c.rank, c.instrument, c.symbol, c.score, c.facts) for c in candidates],
        [(p.rank, p.instrument, p.entry, p.stop, p.target, p.time_stop_days) for p in plans],
    )


def test_a_bar_and_a_corporate_action_after_the_run_day_leave_its_candidates_and_plans_unchanged(
    conn: psycopg.Connection, another_database_url: str, tmp_path: Path
) -> None:
    # BETA, a Candidate, gaps 50 rupees down the next day; GAMMA, another,
    # splits Rs 10 to Rs 5 a week later.
    split_day = date(2023, 6, 22)
    later = MARKET.with_events("BETA", Gap(NEXT_DAY, Decimal("-50.00"))).with_events(
        "GAMMA", Split(split_day, from_face=10, to_face=5)
    )
    fixtures = write_fixtures(later, tmp_path, repo_calendar())
    with app_connection(another_database_url) as other:
        load_market(other, fixtures, later)
        compute_universe_months(other, later, RUN_DAY, RUN_DAY)
        seen_later = backtest_run(other)
        # Both changes are there to be seen by a Run after them.
        (beta,) = queries.adjusted_daily_candles(
            other, "BETA", start=NEXT_DAY, end=NEXT_DAY, as_of=NEXT_DAY
        )
        (gamma,) = queries.adjusted_daily_candles(
            other, "GAMMA", start=RUN_DAY, end=RUN_DAY, as_of=split_day
        )
    assert beta.open == Decimal("430.00")
    assert gamma.close == Decimal("280.00")

    assert seen_later == backtest_run(conn)


def test_the_runs_market_data_refuses_a_read_past_the_as_of_date(conn: psycopg.Connection) -> None:
    data = StoredMarketData(conn, repo_calendar(), as_of=RUN_DAY)

    with pytest.raises(ValueError, match="after the as-of date"):
        data.bars("ALPHA", start=RUN_DAY, end=NEXT_DAY)


@pytest.mark.parametrize(
    ("day", "refusal"),
    [
        (date(2023, 6, 17), "not a trading day"),  # a Saturday
        (date(2023, 7, 3), "no Universe for 2023-07"),  # no month computed
    ],
    ids=["a-closed-day", "a-month-without-a-universe"],
)
def test_a_run_is_refused_on_a_closed_day_or_without_the_months_universe(
    conn: psycopg.Connection, day: date, refusal: str
) -> None:
    with pytest.raises(ValueError, match=refusal):
        run(
            conn,
            PullbackV1(),
            mode="backtest",
            as_of=day,
            calendar=repo_calendar(),
            rule_version=MARKET.rule.version,
            backtest_id=uuid.uuid4(),
            **RISK,
        )
    assert queries.runs(conn, as_of=day) == []
