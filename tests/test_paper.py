"""``run --mode paper``: the paper dry-run, and the proof that the Engine Mode changes nothing
but the clock and the data source (ADR 0002, 0019).

The market is ``synthetic_eod.base_market``. On ``RUN_DAY`` (2023-06-15)
ALPHA, GAMMA and BETA are the Candidates (``test_run.py``); with no sector load
all three are Unclassified, so ALPHA and GAMMA Pass and BETA is Blocked by the
sector Limit (``test_verdicts.py``). Their entries fill at the next day's open
and the targets rest from then, re-placed each day; neither is reached. On
2023-07-03, the tenth trading day after the Fill (2023-06-29 is a holiday),
ALPHA's time stop comes due and GAMMA gaps 60 rupees down past its stop, so both
exits sell at 2023-07-04's open. The trading days from ``RUN_DAY`` to 2023-07-05
are run once as a backtest and once as paper Runs, one a day on a wall clock set
to each day in turn, against the same Strategy, Risk Engine, cost model and fill
model objects.

``gapped_market`` gaps BETA 50 rupees down on 2023-06-19 in a market where
each Stock has a sector, so BETA Passes on ``RUN_DAY``, is stopped on the
completed bar of the gap and sells at the next open: a closed trade in the
first four days, the kind the Paper Gate counts.
"""

import dataclasses
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import psycopg
import pytest

from limits import PULLBACK
from synthetic_eod import (
    EXIT_BAND,
    REPO_ROOT,
    RUN_DAY,
    STARTING_EQUITY,
    Gap,
    base_market,
    compute_universe_months,
    freeze_protocol,
    load_market,
    load_sectors_on,
    repo_calendar,
    repo_costs,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.costs import CostModel
from tradeline.db import app_connection
from tradeline.engine import CollectedMarketData, WallClock, backtest, paper, run
from tradeline.paper_broker import FillModel
from tradeline.risk import RiskEngine
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes

MARKET = base_market()
NEXT_DAY = date(2023, 6, 16)
DAYS = tuple(repo_calendar().trading_days(RUN_DAY, date(2023, 7, 5)))
LIMITS = PULLBACK
TICK_SIZES = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
# A backtest runs under a Protocol: one whose Holdout is the month the days end in.
PROTOCOL_FROM = date(2023, 4, 1)

# Every method through which the engine asks a component to decide, size, price or fill.
SPIED: tuple[tuple[type, str], ...] = (
    (PullbackV1, "screen"),
    (PullbackV1, "plan"),
    (RiskEngine, "assess"),
    (CostModel, "charges"),
    (FillModel, "entry"),
    (FillModel, "exit"),
    (FillModel, "target"),
    (FillModel, "trigger"),
    (FillModel, "exit_limit"),
)


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_fixtures(MARKET, tmp_path_factory.mktemp("synthetic"), repo_calendar())


@pytest.fixture(scope="module")
def loaded(database_url: str, fixtures: Path) -> str:
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, RUN_DAY, DAYS[-1])
    return database_url


@pytest.fixture
def conn(loaded: str) -> Iterator[psycopg.Connection]:
    with app_connection(loaded) as conn:
        yield conn


def on(day: date) -> WallClock:
    """The wall clock read on ``day``."""
    return WallClock(today=lambda: day)


@dataclasses.dataclass
class BothModes:
    """The same days run in backtest and in paper mode, as the Ledger holds them."""

    backtest: dict[str, list[Any]]
    paper: dict[str, list[Any]]
    backtest_runs: list[queries.LedgerRun]
    paper_runs: list[queries.LedgerRun]
    # For each spied method, the objects it was called on in each mode.
    called_on: dict[str, tuple[list[object], list[object]]]
    components: dict[type, object]


def ledger(conn: psycopg.Connection, runs: list[queries.LedgerRun]) -> dict[str, list[Any]]:
    """Everything the Runs and their account wrote, with every row id left out."""
    run = runs[0]
    account: queries.Account = {
        "mode": run.mode,
        "backtest_id": run.backtest_id,
        "dry_run": run.dry_run,
    }

    def without(row: Any, *ids: str) -> tuple[Any, ...]:
        return tuple(v for k, v in dataclasses.asdict(row).items() if k not in ids)

    def per_run(read: Callable[[psycopg.Connection, uuid.UUID], list[Any]]) -> list[Any]:
        return [[without(row, "run_id") for row in read(conn, r.id)] for r in runs]

    return {
        "runs": [(r.as_of, r.universe_size, r.parameters, r.cash, r.equity) for r in runs],
        "candidates": per_run(queries.candidates),
        "plans": per_run(queries.plans),
        "passes": per_run(queries.passes),
        "blocks": per_run(queries.blocks),
        "verdicts": per_run(queries.verdicts),
        "orders": [
            without(o, "id", "run_id", "position_id") for o in queries.orders(conn, **account)
        ],
        "fills": [without(f, "order_id", "position_id") for f in queries.fills(conn, **account)],
        "positions": [without(p, "position_id") for p in queries.position_events(conn, **account)],
    }


@pytest.fixture(scope="module")
def both_modes(loaded: str) -> BothModes:
    strategy, risk, fills, costs = PullbackV1(), RiskEngine(), FillModel(), repo_costs()
    shared: dict[str, Any] = {
        "calendar": repo_calendar(),
        "rule_version": MARKET.rule.version,
        "limits": LIMITS,
        "tick_sizes": TICK_SIZES,
        "costs": costs,
        "risk_engine": risk,
        "fill_model": fills,
    }
    with ExitStack() as stack, app_connection(loaded) as conn:
        spies = {
            f"{cls.__name__}.{name}": stack.enter_context(
                mock.patch.object(cls, name, autospec=True, side_effect=getattr(cls, name))
            )
            for cls, name in SPIED
        }
        frozen = freeze_protocol(conn, MARKET, PROTOCOL_FROM, DAYS[-1], limits=LIMITS)
        unversioned = {k: v for k, v in shared.items() if k not in ("rule_version", "limits")}
        backtest(conn, strategy, protocol=frozen.hash, start=DAYS[0], end=DAYS[-1], **unversioned)
        in_backtest = {name: len(spy.call_args_list) for name, spy in spies.items()}
        for day in DAYS:
            paper(
                conn,
                strategy,
                clock=on(day),
                starting_equity=STARTING_EQUITY,
                band=EXIT_BAND,
                **shared,
            )
        called_on = {
            name: (
                [c.args[0] for c in spy.call_args_list[: in_backtest[name]]],
                [c.args[0] for c in spy.call_args_list[in_backtest[name] :]],
            )
            for name, spy in spies.items()
        }
        backtest_runs = queries.runs(conn, mode="backtest")
        paper_runs = queries.runs(conn, mode="paper")
        return BothModes(
            backtest=ledger(conn, backtest_runs),
            paper=ledger(conn, paper_runs),
            backtest_runs=backtest_runs,
            paper_runs=paper_runs,
            called_on=called_on,
            components={PullbackV1: strategy, RiskEngine: risk, CostModel: costs, FillModel: fills},
        )


def test_the_same_days_in_backtest_and_paper_mode_write_the_same_ledger_but_for_the_mode(
    both_modes: BothModes,
) -> None:
    assert [r.as_of for r in both_modes.backtest_runs] == list(DAYS)
    assert [r.as_of for r in both_modes.paper_runs] == list(DAYS)
    assert {(r.mode, r.dry_run) for r in both_modes.paper_runs} == {("paper", True)}
    assert all(r.backtest_id is None for r in both_modes.paper_runs)
    assert {r.dry_run for r in both_modes.backtest_runs} == {False}

    written = both_modes.backtest
    # Every kind of row is there to compare: ALPHA and GAMMA Pass, fill, have their targets
    # re-placed each day and are sold, one at its time stop, one at its stop; BETA is Blocked.
    assert [len(day) for day in written["passes"]][0] == 2
    assert [len(day) for day in written["blocks"]][0] == 1
    assert [(f[3], f[4], f[0]) for f in written["fills"]] == [
        ("ALPHA", "buy", NEXT_DAY),
        ("GAMMA", "buy", NEXT_DAY),
        ("ALPHA", "sell", date(2023, 7, 4)),
        ("GAMMA", "sell", date(2023, 7, 4)),
    ]
    assert [(p[4], p[-1]) for p in written["positions"] if p[0] == "closed"] == [
        ("ALPHA", "time"),
        ("GAMMA", "stop"),
    ]

    assert both_modes.paper == written


def test_the_strategy_risk_engine_cost_model_and_fill_model_are_the_same_objects_in_both_modes(
    both_modes: BothModes,
) -> None:
    for name, (in_backtest, in_paper) in both_modes.called_on.items():
        component = both_modes.components[next(c for c, n in SPIED if f"{c.__name__}.{n}" == name)]
        assert in_backtest, f"{name} was not called in backtest mode"
        assert in_paper, f"{name} was not called in paper mode"
        assert all(o is component for o in in_backtest + in_paper), name


def test_a_paper_runs_market_data_refuses_a_read_past_the_as_of_date(
    conn: psycopg.Connection,
) -> None:
    data = CollectedMarketData(conn, repo_calendar(), as_of=RUN_DAY)

    with pytest.raises(ValueError, match="after the as-of date"):
        data.bars("ALPHA", start=RUN_DAY, end=NEXT_DAY)


def test_the_wall_clock_refuses_a_day_after_today() -> None:
    with pytest.raises(ValueError, match="after today"):
        list(WallClock(today=lambda: RUN_DAY, day=NEXT_DAY).days())


def test_a_paper_run_is_refused_for_a_day_collect_daily_has_not_loaded(
    conn: psycopg.Connection,
) -> None:
    # The files end on 2024-06-30; the first trading day after has no end-of-day rows.
    with pytest.raises(ValueError, match="run collect-daily"):
        paper(
            conn,
            PullbackV1(),
            clock=on(date(2024, 7, 1)),
            calendar=repo_calendar(),
            rule_version=MARKET.rule.version,
            limits=LIMITS,
            tick_sizes=TICK_SIZES,
            costs=repo_costs(),
            starting_equity=STARTING_EQUITY,
            band=EXIT_BAND,
        )


def test_an_unclassified_member_in_a_paper_run_raises_the_stale_sector_map_alert(
    another_database_url: str, fixtures: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shared: dict[str, Any] = {
        "calendar": repo_calendar(),
        "rule_version": MARKET.rule.version,
        "limits": LIMITS,
        "tick_sizes": TICK_SIZES,
        "costs": repo_costs(),
    }
    with app_connection(another_database_url) as conn:
        load_market(conn, fixtures, MARKET)
        frozen = freeze_protocol(conn, MARKET, PROTOCOL_FROM, RUN_DAY, limits=LIMITS)
        with caplog.at_level(logging.WARNING, logger="tradeline.engine"):
            backtest(
                conn,
                PullbackV1(),
                protocol=frozen.hash,
                start=RUN_DAY,
                end=RUN_DAY,
                calendar=repo_calendar(),
                tick_sizes=TICK_SIZES,
            )
        assert not [r for r in caplog.records if r.getMessage() == "stale sector map"]

        with caplog.at_level(logging.WARNING, logger="tradeline.engine"):
            paper(
                conn,
                PullbackV1(),
                clock=on(RUN_DAY),
                starting_equity=STARTING_EQUITY,
                band=EXIT_BAND,
                **shared,
            )

    (alert,) = [r for r in caplog.records if r.getMessage() == "stale sector map"]
    assert alert.levelno == logging.WARNING
    # No Stock of the base market is on NSE's sector list: the whole Universe is Unclassified.
    assert alert.__dict__["unclassified"] == ["ALPHA", "BETA", "DELTA", "EPSILON", "GAMMA"]
    assert alert.__dict__["as_of"] == RUN_DAY.isoformat()


def gapped_market() -> Any:
    market = MARKET.with_events("BETA", Gap(date(2023, 6, 19), Decimal("-50.00")))
    for symbol, sector in (
        ("ALPHA", "Capital Goods"),
        ("GAMMA", "Healthcare"),
        ("BETA", "Chemicals"),
        ("DELTA", "Power"),
        ("EPSILON", "Textiles"),
        ("OMEGA", "Services"),
    ):
        market = market.with_stock(symbol, sector=sector)
    return market


def test_the_paper_gate_counts_paper_trades_and_never_a_dry_runs(
    fresh_database_url: str, tmp_path: Path
) -> None:
    market = gapped_market()
    fixtures = write_fixtures(market, tmp_path, repo_calendar())
    shared: dict[str, Any] = {
        "calendar": repo_calendar(),
        "rule_version": market.rule.version,
        "limits": LIMITS,
        "tick_sizes": TICK_SIZES,
        "costs": repo_costs(),
        "starting_equity": STARTING_EQUITY,
        "band": EXIT_BAND,
    }
    with app_connection(fresh_database_url) as conn:
        load_market(conn, fixtures, market)
        load_sectors_on(conn, fixtures, RUN_DAY)
        compute_universe_months(conn, market, RUN_DAY, RUN_DAY)
        for day in DAYS[:4]:
            paper(conn, PullbackV1(), clock=on(day), **shared)
            # The same day in the paper account that is not a dry run.
            run(conn, PullbackV1(), mode="paper", as_of=day, **shared)

        dry = queries.positions(conn, mode="paper", backtest_id=None, dry_run=True)
        counted = queries.positions(conn, mode="paper", backtest_id=None, dry_run=False)
        gate = queries.paper_gate_trades(conn)

    # BETA's stop closed one trade in each account.
    assert [(p.symbol, p.exit_reason) for p in dry if p.closed_on] == [("BETA", "stop")]
    assert [(p.symbol, p.exit_reason) for p in counted if p.closed_on] == [("BETA", "stop")]
    assert [p.id for p in gate] == [p.id for p in counted if p.closed_on]


def test_run_in_paper_mode_on_the_command_line_writes_a_dry_run_for_the_day(
    fresh_database_url: str, fixtures: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, RUN_DAY, RUN_DAY)
    rule = MARKET.rule
    path = tmp_path / "tradeline.toml"
    path.write_text(
        f"[universe]\ntop_n = {rule.top_n}\nlookback_trading_days = {rule.lookback_trading_days}\n"
        f"min_listing_trading_days = {rule.min_listing_trading_days}\n"
        f'[integrity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[parity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[costs]\nschedule = "{REPO_ROOT / "cost_rates.csv"}"\n'
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(path))
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)

    assert main(["run", "--mode", "paper", "--date", RUN_DAY.isoformat()]) == 0

    with app_connection(fresh_database_url) as conn:
        (written,) = queries.runs(conn)
        verdicts = queries.verdicts(conn, written.id)
    assert (written.mode, written.backtest_id, written.dry_run) == ("paper", None, True)
    assert written.as_of == RUN_DAY
    assert [(v.symbol, v.verdict) for v in verdicts] == [
        ("ALPHA", "BUY"),
        ("GAMMA", "BUY"),
        ("BETA", "SKIP"),
    ]
