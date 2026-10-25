"""Walk-Forward over a frozen Protocol: ``tradeline backtest --protocol P`` runs it whole and
writes every Window to the Ledger; read back through ``tradeline.queries`` and the report's
JSON.

The fixture is ``synthetic_eod.walk_forward_market`` under ``walk_forward_terms``: three
Windows (Out-of-Sample 2022-07..08, 2022-09..10, 2022-11..12, each with the three months
before In-Sample), a Holdout in 2023-01, the grid k in {2, 3}, r in {2}, n in {5, 10}, a
cell eligible from two in-sample trades, and the sector Limit lifted (no Stock has a
sector, so all would share Unclassified's 30 percent).

Every trade here is sized by the 15 percent position Limit, not by its stop, so k changes
no trade that ends at its time stop: the cells at k = 2 and k = 3 tie, and the tie goes to
the smaller k. A fall's recovery crosses back over the 20-day average about two weeks on,
a pullback of its own: ASH's on 2022-10-11, BIRCH's on 2022-11-09.

- Window 1's In-Sample Window (2022-04..06) closes one trade, ASH's from 2022-05-10: no
  cell is eligible, so the Window makes no selection, and with nothing chosen before it
  the Stitched Curve has no Runs in it.
- Window 2's (2022-06..08) closes two, ASH's from 2022-07-12 and BIRCH's from 2022-08-10,
  both rises: n = 10 holds each twice as long as n = 5 and earns more on the same risk,
  so it is chosen (k = 2, r = 2, n = 10).
- Window 3's (2022-08..10) closes four: BIRCH's from 2022-08-10 (a rise), ASH's from
  2022-09-13 and BIRCH's from 2022-10-11 (falls, which n = 5 sells before and n = 10
  into), and ASH's recovery on 2022-10-11 (a rise): n = 5 is chosen (k = 2, r = 2,
  n = 5).

On the Stitched Curve, Window 2 (2022-09..10) closes ASH's and BIRCH's falls and ASH's
recovery at n = 10, and opens CEDAR on 2022-10-28 from its pullback of 2022-10-27 with a
time stop of 10. Carried into Window 3, chosen at n = 5, it keeps its 10: its tenth
trading day from the Fill is 2022-11-14 (2022-11-08 is a holiday), and the exit fills at
the next open, 2022-11-15. Window 3 (2022-11..12) also closes BIRCH's recovery and
CEDAR's 2022-11-22 pullback at n = 5, and ASH, bought on 2022-12-27 from its pullback of
2022-12-26, is still open at the last out-of-sample day's close, 2022-12-30: its Boundary
Exit sells it there, at that close, after costs.
"""

import contextlib
import io
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from limits import PULLBACK
from synthetic_eod import (
    REPO_ROOT,
    WALK_FORWARD_RANGE,
    config_text,
    freeze_protocol,
    load_market,
    repo_calendar,
    walk_forward_market,
    walk_forward_terms,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.risk import Decision, Limits, Portfolio, Proposal, RiskEngine
from tradeline.strategy import Strategy
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes
from tradeline.walk_forward import walk_forward

D = Decimal
CALENDAR = repo_calendar()
IN_SAMPLE = [
    (date(2022, 4, 1), date(2022, 6, 30)),
    (date(2022, 6, 1), date(2022, 8, 31)),
    (date(2022, 8, 1), date(2022, 10, 31)),
]
OUT_OF_SAMPLE = [
    (date(2022, 7, 1), date(2022, 8, 31)),
    (date(2022, 9, 1), date(2022, 10, 31)),
    (date(2022, 11, 1), date(2022, 12, 31)),
]


def trading_days(span: tuple[date, date]) -> int:
    return len(list(CALENDAR.trading_days(*span)))


@pytest.fixture(scope="module")
def frozen(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """The market loaded and the fixture Protocol frozen, nothing run: the database (each
    run below starts from a copy of it) and the Protocol's hash."""
    market = walk_forward_market()
    fixtures = write_fixtures(market, tmp_path_factory.mktemp("walk_forward"), CALENDAR)
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        found = freeze_protocol(
            conn,
            market,
            *WALK_FORWARD_RANGE,
            terms=walk_forward_terms(),
            limits=replace(PULLBACK, max_sector=D(1)),
        )
    return database_url, found.hash


def protocol_run(database_url: str, protocol: str, directory: Path) -> str:
    """``backtest --protocol`` over the database; the report's JSON text."""
    config = directory / "tradeline.toml"
    config.write_text(config_text(walk_forward_market()))
    out = directory / "walk_forward.json"
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        assert main(["backtest", "--protocol", protocol, "--out", str(out)]) == 0
    return out.read_text()


@pytest.fixture(scope="module")
def ran(
    frozen: tuple[str, str],
    clone_database: Callable[[str], str],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str, str]:
    """The fixture Protocol run whole: its database, its hash and the report's JSON text."""
    source, protocol = frozen
    url = clone_database(source)
    return url, protocol, protocol_run(url, protocol, tmp_path_factory.mktemp("run"))


@pytest.fixture
def conn(ran: tuple[str, str, str]) -> Any:
    with app_connection(ran[0]) as connection:
        yield connection


def stitched(conn: psycopg.Connection, protocol: str) -> uuid.UUID:
    found = queries.walk_forward(conn, protocol)
    assert found is not None
    return found.backtest_id


def test_every_window_records_its_selection_eligible_cells_and_out_of_sample_result(
    conn: psycopg.Connection, ran: tuple[str, str, str]
) -> None:
    _, protocol, _ = ran

    first, second, third = queries.walk_forward_windows(conn, protocol)

    assert (first.cells, first.eligible_cells, first.parameters, first.in_sample_trades) == (
        4,
        0,
        None,
        None,
    )
    assert (first.out_of_sample_runs, first.out_of_sample_trades, first.start_equity) == (
        0,
        0,
        None,
    )
    assert (second.cells, second.eligible_cells, second.in_sample_trades) == (4, 4, 2)
    assert second.parameters == {"k": "2", "r": "2", "n": "10"}
    assert (second.out_of_sample_runs, second.out_of_sample_trades) == (
        trading_days(OUT_OF_SAMPLE[1]),
        3,
    )
    assert second.start_equity == D(500000)
    assert (third.cells, third.eligible_cells, third.in_sample_trades) == (4, 4, 4)
    assert third.parameters == {"k": "2", "r": "2", "n": "5"}
    # CEDAR carried in, BIRCH's recovery, CEDAR's pullback, ASH's Boundary Exit.
    assert (third.out_of_sample_runs, third.out_of_sample_trades) == (
        trading_days(OUT_OF_SAMPLE[2]),
        4,
    )
    assert third.start_equity == second.end_equity
    runs = queries.runs(conn, mode="backtest", backtest_id=stitched(conn, protocol))
    assert third.end_equity == runs[-1].equity


def test_grid_cells_keep_only_a_summary_and_only_the_stitched_curve_has_ledger_rows(
    conn: psycopg.Connection, ran: tuple[str, str, str]
) -> None:
    _, protocol, _ = ran

    cells = queries.walk_forward_cells(conn, protocol)

    assert [(c.window, c.runs) for c in cells] == [
        (window, trading_days(IN_SAMPLE[window - 1])) for window in (1, 2, 3) for _ in range(4)
    ]
    assert {json.dumps(c.parameters, sort_keys=True) for c in cells} == {
        json.dumps({"k": k, "r": "2", "n": n}, sort_keys=True)
        for k in ("2", "3")
        for n in ("5", "10")
    }
    assert [c.trades for c in cells] == [1] * 4 + [2] * 4 + [4] * 4
    # Every backtest Run in the Ledger is the Stitched Curve's, on an out-of-sample day.
    runs = queries.runs(conn, mode="backtest")
    assert {r.backtest_id for r in runs} == {stitched(conn, protocol)}
    assert len(runs) == trading_days(OUT_OF_SAMPLE[1]) + trading_days(OUT_OF_SAMPLE[2])


@pytest.mark.parametrize(
    ("protocol", "refusal"),
    [(None, "names none"), ("0" * 64, "no Protocol 0{64} is frozen")],
)
def test_walk_forward_runs_only_under_a_frozen_protocol(
    conn: psycopg.Connection, protocol: str | None, refusal: str
) -> None:
    with pytest.raises(ValueError, match=refusal):
        walk_forward(
            conn,
            pullback,
            protocol=protocol,
            calendar=CALENDAR,
            tick_sizes=load_tick_sizes(REPO_ROOT / "tick_sizes.csv"),
        )


@pytest.mark.parametrize("table", ["walk_forwards", "walk_forward_cells", "walk_forward_windows"])
def test_the_application_role_cannot_change_or_remove_what_walk_forward_recorded(
    conn: psycopg.Connection, table: str
) -> None:
    for statement in (f"UPDATE {table} SET protocol = protocol", f"DELETE FROM {table}"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()


def test_a_window_where_no_cell_is_eligible_makes_no_selection_and_the_report_says_so(
    ran: tuple[str, str, str],
) -> None:
    _, _, text = ran

    first, second, _ = json.loads(text)["windows"]

    assert first["selection"] is None
    assert first["no_selection"] == "no cell reached 2 in-sample trades"
    assert first["out_of_sample_result"]["runs"] == 0
    assert second["selection"]["parameters"] == {"k": "2", "n": "10", "r": "2"}
    assert second["no_selection"] is None


def test_a_position_carried_across_a_window_boundary_keeps_its_stop_target_and_time_stop(
    conn: psycopg.Connection, ran: tuple[str, str, str]
) -> None:
    _, protocol, _ = ran
    account = stitched(conn, protocol)

    (cedar,) = (
        p
        for p in queries.positions(conn, mode="backtest", backtest_id=account)
        if p.symbol == "CEDAR" and p.opened_on == date(2022, 10, 28)
    )

    (granted,) = (p for p in queries.passes(conn, cedar.run_id) if p.rank == cedar.rank)
    assert (cedar.stop, cedar.target, cedar.time_stop_days) == (
        granted.stop,
        granted.target,
        10,
    )
    assert (cedar.as_of, cedar.exit_reason) == (date(2022, 11, 15), "time")
    # The Window it was carried into ran at n = 5.
    (boundary_day,) = queries.runs(
        conn, mode="backtest", backtest_id=account, as_of=date(2022, 11, 1)
    )
    assert boundary_day.parameters["n"] == "5"


def test_every_position_open_at_the_last_out_of_sample_day_takes_a_boundary_exit_with_costs(
    conn: psycopg.Connection, ran: tuple[str, str, str]
) -> None:
    _, protocol, text = ran
    account = stitched(conn, protocol)
    last = date(2022, 12, 30)
    (ash_close,) = (
        b.close
        for b in walk_forward_market()
        .stock("ASH")
        .path_bars(walk_forward_market().trading_days(CALENDAR))
        if b.day == last
    )

    (ash,) = (
        p
        for p in queries.positions(conn, mode="backtest", backtest_id=account)
        if p.opened_on == date(2022, 12, 27)
    )

    assert (ash.symbol, ash.as_of, ash.exit_reason, ash.exit_price) == (
        "ASH",
        last,
        "boundary",
        ash_close,
    )
    (sale,) = (
        f
        for f in queries.fills(conn, mode="backtest", backtest_id=account)
        if f.position_id == ash.id and f.side == "sell"
    )
    assert (sale.day, sale.price) == (last, ash_close)
    assert sale.charges is not None and sale.charges.total > 0
    assert queries.positions(conn, mode="backtest", backtest_id=account, open_only=True) == []
    assert queries.orders(conn, mode="backtest", backtest_id=account, open_only=True) == []
    curve = json.loads(text)["stitched_curve"]
    assert (curve["boundary_exits"], curve["metrics"]["open_positions"]) == (1, 0)
    assert curve["to"] == last.isoformat()


def test_two_runs_of_the_fixture_protocol_give_byte_identical_report_json(
    ran: tuple[str, str, str],
    frozen: tuple[str, str],
    clone_database: Callable[[str], str],
    tmp_path: Path,
) -> None:
    _, protocol, text = ran
    source, _ = frozen

    again = protocol_run(clone_database(source), protocol, tmp_path)

    assert again == text


class Interrupted(Exception):
    """The run stopped from outside, like a process killed."""


class InterruptingRiskEngine(RiskEngine):
    """The Risk Engine, until its ``at``-th assessment (one a Run), which interrupts."""

    def __init__(self, at: int) -> None:
        self.at, self.calls = at, 0

    def assess(
        self,
        proposals: Sequence[Proposal],
        *,
        portfolio: Portfolio,
        limits: Limits,
        run_id: uuid.UUID,
    ) -> list[Decision]:
        self.calls += 1
        if self.calls == self.at:
            raise Interrupted
        return super().assess(proposals, portfolio=portfolio, limits=limits, run_id=run_id)


def pullback(parameters: Mapping[str, Decimal]) -> Strategy:
    return PullbackV1(k=parameters["k"], r=parameters["r"], n=int(parameters["n"]))


def test_an_interrupted_run_resumes_at_the_first_unfinished_cell_with_the_same_result(
    ran: tuple[str, str, str],
    frozen: tuple[str, str],
    clone_database: Callable[[str], str],
) -> None:
    _, protocol, text = ran
    url = clone_database(frozen[0])
    tick_sizes = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
    cells = [trading_days(span) for span in IN_SAMPLE]

    def run(risk_engine: RiskEngine) -> Any:
        with app_connection(url) as conn:
            return walk_forward(
                conn,
                pullback,
                protocol=protocol,
                calendar=CALENDAR,
                tick_sizes=tick_sizes,
                risk_engine=risk_engine,
            )

    # Twenty days into Window 2's Out-of-Sample run, after all eight cells of Windows 1 and 2.
    with pytest.raises(Interrupted):
        run(InterruptingRiskEngine(4 * cells[0] + 4 * cells[1] + 21))
    with app_connection(url) as conn:
        assert len(queries.walk_forward_cells(conn, protocol)) == 8
        assert [w.window for w in queries.walk_forward_windows(conn, protocol)] == [1]
        assert len(queries.runs(conn, mode="backtest", backtest_id=stitched(conn, protocol))) == 20
    # Then in Window 3's second cell, after Window 2's remaining days and its first cell.
    remaining = trading_days(OUT_OF_SAMPLE[1]) - 20
    with pytest.raises(Interrupted):
        run(InterruptingRiskEngine(remaining + cells[2] + 30))
    with app_connection(url) as conn:
        assert len(queries.walk_forward_cells(conn, protocol, window=3)) == 1
        assert [w.window for w in queries.walk_forward_windows(conn, protocol)] == [1, 2]

    finished = run(RiskEngine())

    assert finished.cells_run == 3
    assert finished.report.text() == text
