"""Walk-Forward: a frozen Protocol run whole, unattended, every Window written to the Ledger.

For each Window in order, every cell of the Protocol's grid runs the In-Sample
Window as its own backtest from the Protocol's starting Equity and keeps only
a summary (``walk_forward_cells``): its Runs, Orders, Fills and Positions are
written in a transaction that is rolled back. ``choose`` then picks the cell
with the highest neighbour-averaged in-sample Sharpe among those with at least
the Protocol's minimum in-sample trades; a Window with no eligible cell makes
no selection. The chosen cell runs the Out-of-Sample Window with every row
kept, continuing the Stitched Curve: one account from the starting Equity
under one backtest id (``walk_forwards``), its Positions carried across Window
boundaries with the stop, target and time stop they were opened with. A Window
that makes no selection plans no new Position, but the account still steps
its days (its Runs recording the last chosen parameters), so a Position
carried into it keeps its exits; before any Window
has chosen, the account holds nothing and has no Runs. At the last
out-of-sample day's close every open Position takes a Boundary Exit, after
costs. Each Window's selection and out-of-sample result is then recorded
(``walk_forward_windows``).

A Protocol whose grid has one cell (``lowvol_v1``'s, which has no free
parameter) has nothing to select: no In-Sample run is made, every Window
records no selection, and its Out-of-Sample Windows run at the one cell, with
entries, as one continuous backtest, the Stitched Curve (a portfolio
Strategy's starting with its Initial Buy, ``engine.backtest_span``).

The run is resumable per cell: a cell's summary and each out-of-sample day are
committed as they finish, so a run interrupted and started again skips what is
recorded, picks up at the first unfinished cell (or the day after the
account's last Run), and ends with the same result.

What makes it fast enough: the data snapshot is verified once, not per cell;
the cells of a Window and its Out-of-Sample run share one ``SeriesCache``, so
the Window's bars load once and each day's market (the screen's Candidates,
sectors, traded values, tick reference closes) is read once between them; and
the engine pipelines each Run's writes, since a cell is bound by round trips
to Postgres more than by its own work.

``choose`` is pure.
"""

from __future__ import annotations

import itertools
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.engine import SeriesCache, backtest_span
from tradeline.freeze import verify_snapshot
from tradeline.metrics import RelativeDrawdownDay, max_drawdown, sharpe
from tradeline.paper_broker import FILL_MODEL, FillModel
from tradeline.protocol import TIE_BREAK, Protocol, Window
from tradeline.report import (
    FRACTION,
    RATIO,
    RUPEE,
    Account,
    read_account,
    relative_drawdown_days_json,
    relative_drawdown_days_of,
    relative_drawdown_json,
    rounded,
)
from tradeline.risk import RISK_ENGINE, RiskEngine
from tradeline.strategy.portfolio import AnyStrategy
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)

# The axes the tie-break prefers smaller values of, in its order: k, then r, then n.
SMALLER_FIRST = tuple(t.removeprefix("smaller ") for t in TIE_BREAK if t.startswith("smaller "))

# A Strategy at one cell's parameters, by the names its ``parameters`` gives them.
StrategyFactory = Callable[[Mapping[str, Decimal]], AnyStrategy]
# What a Window of a one-cell Protocol's Walk-Forward records in place of a selection.
ONE_CELL = (
    "the grid has one cell, so Walk-Forward selects nothing: the Out-of-Sample Windows run"
    " at it as one continuous backtest"
)


@dataclass(frozen=True)
class CellScore:
    """One grid cell's In-Sample run as Walk-Forward reads it: its parameters, its closed
    trades and its Sharpe after costs (None when the curve gives none)."""

    parameters: Mapping[str, Decimal]
    trades: int
    sharpe: Decimal | None


@dataclass(frozen=True)
class Selection:
    """A Window's choice: the chosen cell's parameters, objective, Sharpe and trades, or
    None for each when no cell was eligible; and how many cells were."""

    parameters: Mapping[str, Decimal] | None
    objective: Decimal | None
    sharpe: Decimal | None
    trades: int | None
    eligible: int


def choose(
    cells: Sequence[CellScore], grid: Mapping[str, tuple[Decimal, ...]], *, min_trades: int
) -> Selection:
    """The cell with the highest objective among those with at least ``min_trades`` trades:
    the mean of its own Sharpe and its grid neighbours' along each axis (a neighbour without
    a Sharpe left out). Ties go to the higher raw Sharpe, then more trades, then smaller k,
    r and n. A cell without a Sharpe is never chosen."""
    by_key = {_key(c.parameters, grid): c for c in cells}
    if len(by_key) != len(cells):
        raise ValueError("two cells share parameters")
    ranked = []
    for key, cell in by_key.items():
        if cell.trades < min_trades or cell.sharpe is None:
            continue
        sharpes = [cell.sharpe]
        for axis, index in enumerate(key):
            for step in (-1, 1):
                near = by_key.get(key[:axis] + (index + step,) + key[axis + 1 :])
                if near is not None and near.sharpe is not None:
                    sharpes.append(near.sharpe)
        objective = sum(sharpes, Decimal(0)) / len(sharpes)
        smaller = tuple(cell.parameters[a] for a in SMALLER_FIRST)
        ranked.append(((-objective, -cell.sharpe, -cell.trades, smaller), objective, cell))
    if not ranked:
        return Selection(None, None, None, None, 0)
    _, objective, best = min(ranked, key=lambda r: r[0])
    return Selection(dict(best.parameters), objective, best.sharpe, best.trades, len(ranked))


def _key(
    parameters: Mapping[str, Decimal], grid: Mapping[str, tuple[Decimal, ...]]
) -> tuple[int, ...]:
    """The cell's place on each axis of the grid, in the grid's axis order."""
    if set(parameters) != set(grid):
        raise ValueError(f"cell parameters {sorted(parameters)} are not the grid's {sorted(grid)}")
    return tuple(grid[axis].index(parameters[axis]) for axis in grid)


def grid_cells(grid: Mapping[str, tuple[Decimal, ...]]) -> list[dict[str, Decimal]]:
    """Every cell of the grid, smaller k first, then r, then n (other axes after, by name)."""
    axes = [a for a in SMALLER_FIRST if a in grid] + sorted(set(grid) - set(SMALLER_FIRST))
    return [
        dict(zip(axes, values, strict=True))
        for values in itertools.product(*(sorted(grid[a]) for a in axes))
    ]


def one_cell(grid: Mapping[str, tuple[Decimal, ...]]) -> Mapping[str, Decimal] | None:
    """The grid's one cell, when it has exactly one (Walk-Forward then selects nothing);
    None otherwise."""
    cells = grid_cells(grid)
    return cells[0] if len(cells) == 1 else None


@dataclass(frozen=True)
class WalkForwardReport:
    """A Protocol's Walk-Forward read back from the Ledger: each Window's selection and
    out-of-sample result, and the Stitched Curve scored. Under a Protocol with a Relative
    Drawdown Halt it lists, right after the Strategy, every day of the Stitched Curve that
    Halt would have fired, with that day's Relative Drawdown (a backtest never Halts). Its
    JSON holds no backtest id and no timing, so two runs of one Protocol over the same data
    read the same."""

    frozen: queries.FrozenProtocol
    windows: tuple[queries.WalkForwardWindow, ...]
    stitched: Account | None
    # The Stitched Curve's Relative Drawdown Halt days; None without that Halt.
    relative_drawdown_days: tuple[RelativeDrawdownDay, ...] | None = None

    def as_json(self) -> dict[str, Any]:
        terms = self.frozen.protocol
        stitched = self.stitched
        halt: dict[str, Any] = {}
        relative = terms.halts.relative_drawdown
        if relative is not None and self.relative_drawdown_days is not None:
            halt["halt_would_have_fired"] = {
                **relative_drawdown_json(relative),
                "days": relative_drawdown_days_json(self.relative_drawdown_days),
            }
        return {
            "protocol": self.frozen.hash,
            "strategy": {
                "family": terms.family,
                "name": terms.name,
                "code_revision": terms.code_revision,
            },
            **halt,
            "min_in_sample_trades": terms.min_in_sample_trades,
            "windows": [
                window_json(number, window, found, terms)
                for number, (window, found) in enumerate(
                    zip(terms.windows, self.windows, strict=True), start=1
                )
            ],
            "stitched_curve": None
            if stitched is None
            else {
                "from": stitched.days[0].isoformat(),
                "to": stitched.days[-1].isoformat(),
                "trading_days": len(stitched.days),
                "metrics": stitched.scores(risk_free_rate=terms.risk_free_rate).as_json(),
                "boundary_exits": sum(t.exit_reason == "boundary" for t in stitched.closed),
                "closed_positions": [t.as_json() for t in stitched.closed],
            },
        }

    def text(self) -> str:
        return json.dumps(self.as_json(), indent=2) + "\n"


def window_json(
    number: int, window: Window, found: queries.WalkForwardWindow, terms: Protocol
) -> dict[str, Any]:
    selection: dict[str, Any] | None = None
    if found.parameters is not None:
        selection = {
            "parameters": dict(sorted(found.parameters.items())),
            "objective": rounded(found.objective, RATIO),
            "in_sample_sharpe": rounded(found.in_sample_sharpe, RATIO),
            "in_sample_trades": found.in_sample_trades,
        }
    return {
        "window": number,
        "in_sample": [window.in_sample.first.isoformat(), window.in_sample.last.isoformat()],
        "out_of_sample": [
            window.out_of_sample.first.isoformat(),
            window.out_of_sample.last.isoformat(),
        ],
        "cells": found.cells,
        "eligible_cells": found.eligible_cells,
        "selection": selection,
        "no_selection": None
        if selection is not None
        else ONE_CELL
        if one_cell(terms.grid) is not None
        else f"no cell reached {terms.min_in_sample_trades} in-sample trades",
        "out_of_sample_result": {
            "runs": found.out_of_sample_runs,
            "trades": found.out_of_sample_trades,
            "start_equity": rounded(found.start_equity, RUPEE),
            "end_equity": rounded(found.end_equity, RUPEE),
            "return": None
            if found.start_equity is None or found.end_equity is None
            else rounded(found.end_equity / found.start_equity - 1, FRACTION),
        },
    }


@dataclass(frozen=True)
class WalkForwardResult:
    report: WalkForwardReport
    backtest_id: uuid.UUID
    # Cells run by this call (not those an earlier, interrupted call recorded).
    cells_run: int
    seconds: float

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": self.report.frozen.hash,
            "backtest_id": str(self.backtest_id),
            "cells_run": self.cells_run,
            "windows": len(self.report.windows),
            "selected": sum(w.parameters is not None for w in self.report.windows),
            "seconds": round(self.seconds, 1),
        }


def walk_forward(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    *,
    protocol: str | None,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> WalkForwardResult:
    """Run the frozen Protocol ``protocol`` (its hash, or 12 or more characters of it) whole,
    or what is left of it after an interrupted run, and read it back. ``strategy`` makes the
    Strategy at a cell's parameters."""
    began = time.monotonic()
    if not protocol:
        raise ValueError(
            "Walk-Forward runs under a frozen Protocol and names none;"
            " freeze one with `tradeline protocol freeze` and name it"
        )
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(
            f"no Protocol {protocol} is frozen; `tradeline protocol freeze` freezes one"
        )
    terms = frozen.protocol
    # The snapshot once for every cell and Window: the In-Sample and Out-of-Sample days.
    verify_snapshot(conn, frozen, start=terms.first, end=terms.windows[-1].out_of_sample.last)
    backtest_id = _stitched_account(conn, frozen.hash)
    done = {w.window: w for w in queries.walk_forward_windows(conn, frozen.hash)}
    chosen_before: Mapping[str, Decimal] | None = None
    cells_run = 0
    run = _Runner(conn, strategy, frozen, calendar, tick_sizes, risk_engine, fill_model)
    only = one_cell(terms.grid)
    for number, window in enumerate(terms.windows, start=1):
        if number in done:
            if (recorded := done[number].parameters) is not None:
                chosen_before = {a: Decimal(v) for a, v in recorded.items()}
            continue
        last_window = number == len(terms.windows)
        if only is not None:
            cache = SeriesCache(
                conn, start=window.out_of_sample.first, end=window.out_of_sample.last
            )
            run.out_of_sample(backtest_id, window, cache, only, entries=True, last=last_window)
            _record_window(
                conn,
                frozen,
                backtest_id,
                number,
                window,
                cells=1,
                selection=Selection(None, None, None, None, 0),
            )
            continue
        cache = SeriesCache(conn, start=window.in_sample.first, end=window.out_of_sample.last)
        scores, ran = run.cells(number, window, cache)
        cells_run += ran
        selection = choose(scores, terms.grid, min_trades=terms.min_in_sample_trades)
        log.info(
            "walk-forward window chosen",
            extra={
                "window": number,
                "eligible_cells": selection.eligible,
                "parameters": None
                if selection.parameters is None
                else _strings(selection.parameters),
            },
        )
        if selection.parameters is not None:
            run.out_of_sample(
                backtest_id, window, cache, selection.parameters, entries=True, last=last_window
            )
            chosen_before = selection.parameters
        elif chosen_before is not None:
            # Nothing new is planned; what the account holds keeps its exits.
            run.out_of_sample(
                backtest_id, window, cache, chosen_before, entries=False, last=last_window
            )
        _record_window(
            conn,
            frozen,
            backtest_id,
            number,
            window,
            cells=len(scores),
            selection=selection,
        )
    stitched = None
    halt_days: list[RelativeDrawdownDay] | None = None
    if queries.runs(conn, mode="backtest", backtest_id=backtest_id):
        stitched = read_account(conn, backtest_id)
        halt_days = relative_drawdown_days_of(conn, terms, stitched)
    report = WalkForwardReport(
        frozen,
        tuple(queries.walk_forward_windows(conn, frozen.hash)),
        stitched,
        None if halt_days is None else tuple(halt_days),
    )
    result = WalkForwardResult(report, backtest_id, cells_run, time.monotonic() - began)
    log.info("walk-forward finished", extra=result.summary())
    return result


def restitch(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    frozen: queries.FrozenProtocol,
    *,
    backtest_id: uuid.UUID,
    impact: Decimal,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> None:
    """The finished Walk-Forward's Stitched Curve written again in the account
    ``backtest_id`` with the Slippage impact c at ``impact``: each Window's Out-of-Sample
    days at the parameters it chose (nothing new planned where it chose none, as the
    Stitched Curve runs), no cell run again and nothing chosen again, so the c sensitivity
    differs from the Stitched Curve in its costs alone. Resumed from the account's last
    Run. The caller verifies the data snapshot."""
    terms = frozen.protocol
    windows = queries.walk_forward_windows(conn, frozen.hash)
    if len(windows) < len(terms.windows):
        raise ValueError(
            f"the Walk-Forward under Protocol {frozen.hash[:12]} has finished"
            f" {len(windows)} of {len(terms.windows)} Windows"
        )
    run = _Runner(conn, strategy, frozen, calendar, tick_sizes, risk_engine, fill_model)
    only = one_cell(terms.grid)
    chosen: Mapping[str, Decimal] | None = only
    for number, (window, found) in enumerate(zip(terms.windows, windows, strict=True), start=1):
        entries = found.parameters is not None or only is not None
        if found.parameters is not None:
            chosen = {a: Decimal(v) for a, v in found.parameters.items()}
        if chosen is None:
            continue
        cache = SeriesCache(conn, start=window.out_of_sample.first, end=window.out_of_sample.last)
        run.out_of_sample(
            backtest_id,
            window,
            cache,
            chosen,
            entries=entries,
            last=number == len(terms.windows),
            impact=impact,
        )


class _Runner:
    """The cells and Out-of-Sample stretches of one Protocol's Walk-Forward."""

    def __init__(
        self,
        conn: psycopg.Connection,
        strategy: StrategyFactory,
        frozen: queries.FrozenProtocol,
        calendar: TradingCalendar,
        tick_sizes: TickSizes,
        risk_engine: RiskEngine,
        fill_model: FillModel,
    ) -> None:
        self._conn = conn
        self._strategy = strategy
        self._frozen = frozen
        self._span: dict[str, Any] = {
            "calendar": calendar,
            "tick_sizes": tick_sizes,
            "risk_engine": risk_engine,
            "fill_model": fill_model,
        }

    def cells(self, number: int, window: Window, cache: SeriesCache) -> tuple[list[CellScore], int]:
        """Every grid cell's score in the Window, run now unless already recorded; and how
        many were run now."""
        conn, frozen = self._conn, self._frozen
        recorded = {
            _cell_key(c.parameters): c
            for c in queries.walk_forward_cells(conn, frozen.hash, window=number)
        }
        scores, ran = [], 0
        for parameters in grid_cells(frozen.protocol.grid):
            found = recorded.get(_cell_key(_strings(parameters)))
            if found is None:
                found = self._cell(number, window, cache, parameters)
                ran += 1
            scores.append(CellScore(parameters, found.trades, found.sharpe))
        return scores, ran

    def _cell(
        self, number: int, window: Window, cache: SeriesCache, parameters: Mapping[str, Decimal]
    ) -> queries.WalkForwardCell:
        """One cell's In-Sample run, kept as its summary only."""
        conn, terms = self._conn, self._frozen.protocol
        began = time.monotonic()
        backtest_id = uuid.uuid4()
        with conn.transaction(force_rollback=True):
            results = backtest_span(
                conn,
                self._strategy(parameters),
                self._frozen,
                start=window.in_sample.first,
                end=window.in_sample.last,
                backtest_id=backtest_id,
                cache=cache,
                commit=False,
                **self._span,
            )
            trades = sum(
                p.exit_reason is not None
                for p in queries.positions(conn, mode="backtest", backtest_id=backtest_id)
            )
        # From the starting Equity, as the report reads a curve (``report.Account``).
        curve = [terms.starting_equity, *(r.equity for r in results)]
        cell = queries.WalkForwardCell(
            protocol=self._frozen.hash,
            window=number,
            parameters=_strings(parameters),
            runs=len(results),
            trades=trades,
            sharpe=sharpe(curve, risk_free_rate=terms.risk_free_rate),
            max_drawdown=max_drawdown(curve),
            ending_equity=curve[-1],
        )
        with conn.transaction():
            conn.execute(
                "INSERT INTO walk_forward_cells (protocol, window_number, parameters, runs,"
                " trades, sharpe, max_drawdown, ending_equity)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    cell.protocol,
                    cell.window,
                    json.dumps(cell.parameters, sort_keys=True),
                    cell.runs,
                    cell.trades,
                    cell.sharpe,
                    cell.max_drawdown,
                    cell.ending_equity,
                ),
            )
        conn.commit()
        log.info(
            "walk-forward cell recorded",
            extra={
                "window": number,
                "parameters": cell.parameters,
                "trades": cell.trades,
                "sharpe": None if cell.sharpe is None else str(cell.sharpe.quantize(RATIO)),
                "seconds": round(time.monotonic() - began, 1),
            },
        )
        return cell

    def out_of_sample(
        self,
        backtest_id: uuid.UUID,
        window: Window,
        cache: SeriesCache,
        parameters: Mapping[str, Decimal],
        *,
        entries: bool,
        last: bool,
        impact: Decimal | None = None,
    ) -> None:
        """The Window's Out-of-Sample days on the Stitched Curve from the day after the
        account's last Run, each committed as it is written; a Boundary Exit on its last day
        when it is the last Window's. ``impact`` is ``engine.backtest``'s."""
        conn = self._conn
        span = window.out_of_sample
        runs = queries.runs(conn, mode="backtest", backtest_id=backtest_id)
        start = span.first
        if runs and runs[-1].as_of >= span.first:
            # Resumed: the days not yet written.
            calendar: TradingCalendar = self._span["calendar"]
            remaining = [
                d for d in calendar.trading_days(span.first, span.last) if d > runs[-1].as_of
            ]
            if not remaining:
                return
            start = remaining[0]
        began = time.monotonic()
        backtest_span(
            conn,
            self._strategy(parameters),
            self._frozen,
            start=start,
            end=span.last,
            backtest_id=backtest_id,
            cache=cache,
            entries=entries,
            boundary=last,
            impact=impact,
            **self._span,
        )
        log.info(
            "walk-forward out-of-sample written",
            extra={
                "from": start.isoformat(),
                "to": span.last.isoformat(),
                "parameters": _strings(parameters),
                "entries": entries,
                "seconds": round(time.monotonic() - began, 1),
            },
        )


def _stitched_account(conn: psycopg.Connection, protocol: str) -> uuid.UUID:
    """The Stitched Curve's backtest id: the one the Protocol's Walk-Forward started with,
    or a new one."""
    found = queries.walk_forward(conn, protocol)
    if found is not None:
        return found.backtest_id
    with conn.transaction():
        conn.execute(
            "INSERT INTO walk_forwards (protocol, backtest_id) VALUES (%s, %s)",
            (protocol, uuid.uuid4()),
        )
    conn.commit()
    started = queries.walk_forward(conn, protocol)
    assert started is not None
    return started.backtest_id


def _record_window(
    conn: psycopg.Connection,
    frozen: queries.FrozenProtocol,
    backtest_id: uuid.UUID,
    number: int,
    window: Window,
    *,
    cells: int,
    selection: Selection,
) -> None:
    """The Window's selection and its stretch of the Stitched Curve, read from the Ledger."""
    span = window.out_of_sample
    runs = queries.runs(conn, mode="backtest", backtest_id=backtest_id)
    before = [r for r in runs if r.as_of < span.first]
    inside = [r for r in runs if span.first <= r.as_of <= span.last]
    closed = sum(
        p.exit_reason is not None and span.first <= p.as_of <= span.last
        for p in queries.positions(conn, mode="backtest", backtest_id=backtest_id)
    )
    start_equity = end_equity = None
    if inside:
        start_equity = before[-1].equity if before else frozen.protocol.starting_equity
        end_equity = inside[-1].equity
    with conn.transaction():
        conn.execute(
            "INSERT INTO walk_forward_windows (protocol, window_number, cells, eligible_cells,"
            " parameters, objective, in_sample_sharpe, in_sample_trades, out_of_sample_runs,"
            " out_of_sample_trades, start_equity, end_equity)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                frozen.hash,
                number,
                cells,
                selection.eligible,
                None
                if selection.parameters is None
                else json.dumps(_strings(selection.parameters), sort_keys=True),
                selection.objective,
                selection.sharpe,
                selection.trades,
                len(inside),
                closed,
                start_equity,
                end_equity,
            ),
        )
    conn.commit()


def _strings(parameters: Mapping[str, Decimal]) -> dict[str, str]:
    """Parameters as the Ledger holds them: decimal strings by name."""
    return {name: str(value) for name, value in parameters.items()}


def _cell_key(parameters: Mapping[str, str]) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted((name, Decimal(value)) for name, value in parameters.items()))
