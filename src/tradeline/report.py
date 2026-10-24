"""The single-run report: one backtest scored the way the Backtest Gate will score it.

``report_backtest`` backtests one cell (one Strategy at fixed parameters) over a
range, then again with the Slippage impact c at zero and at twice its value, and
reads all three back from the Ledger (``tradeline.queries``); the report is that
reading and nothing else. Its metrics are ``tradeline.metrics`` over the daily
marked Equity curve (each Run's Equity) and the closed trades.

A closed trade is a Position the backtest closed; its net P&L is what its Fills
moved into cash after every charge and Slippage, plus any cash paid in lieu of a
fractional share when it was restated. Its Equity at entry is the Equity its Pass
sized it against. A Position still open on the last day is marked at that day's
adjusted close and listed as open, never counted as a closed trade.

Beside the metrics the report lists every Gapped Stop (a stop exit filled under
the stop, so the trade lost more than its Plan risked) with its realised loss,
every day the daily-loss or drawdown Halt would have fired with the drawdown
then (near the top: a backtest never Halts), the Candidates beyond the Passes
(Blocked by the entry cap, ``max_new_entries``), the Blocks caused by
Unclassified (the sector Limit on Instruments with no recorded sector), every
Block by Limit, and whether real contract notes check the cost model.

The JSON holds no backtest id and no timing, so one cell reported twice over the
same data reads the same; the ids and runtimes are logged.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.costs import CostModel
from tradeline.engine import STARTING_EQUITY, backtest
from tradeline.metrics import (
    HaltDay,
    HaltLimits,
    Trade,
    expectancy,
    halt_days,
    max_drawdown,
    profit_factor,
    sharpe,
)
from tradeline.risk import UNCLASSIFIED, Limits
from tradeline.strategy import Strategy
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar

RUPEE = Decimal("0.01")
FRACTION = Decimal("0.000001")
RATIO = Decimal("0.0001")
# How far back the last day's close of an open Position is looked for.
MARK_LOOKBACK = timedelta(days=31)


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    instrument: str
    opened_on: date
    closed_on: date
    shares: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit_price: Decimal
    exit_reason: str
    net_pnl: Decimal
    equity_at_entry: Decimal

    @property
    def gapped(self) -> bool:
        """A Gapped Stop: the stop's exit filled under the stop."""
        return self.exit_reason == "stop" and self.exit_price < self.stop

    @property
    def planned_risk(self) -> Decimal:
        return self.shares * (self.entry - self.stop)


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    instrument: str
    opened_on: date
    shares: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    # The last day's adjusted close it is marked at.
    close: Decimal

    @property
    def value(self) -> Decimal:
        return self.shares * self.close

    @property
    def unrealised(self) -> Decimal:
        return self.shares * (self.close - self.entry)


@dataclass(frozen=True)
class Scores:
    """A backtest's metrics, as the Backtest Gate reads them."""

    trades: int
    sharpe: Decimal | None
    max_drawdown: Decimal
    profit_factor: Decimal | None
    expectancy_rupees: Decimal | None
    expectancy_fraction: Decimal | None
    ending_equity: Decimal
    open_positions: int

    def as_json(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "sharpe": _text(self.sharpe, RATIO),
            "max_drawdown": _text(self.max_drawdown, FRACTION),
            "profit_factor": _text(self.profit_factor, RATIO),
            "expectancy": {
                "rupees": _text(self.expectancy_rupees, RUPEE),
                "fraction_of_equity": _text(self.expectancy_fraction, FRACTION),
            },
            "ending_equity": _text(self.ending_equity, RUPEE),
            "open_positions": self.open_positions,
        }


@dataclass(frozen=True)
class Account:
    """One backtest read back from the Ledger: its daily curve, its closed trades, its open
    Positions marked at the last close, and its Blocks."""

    days: tuple[date, ...]
    curve: tuple[Decimal, ...]
    closed: tuple[ClosedTrade, ...]
    open: tuple[OpenPosition, ...]
    blocks: tuple[queries.LedgerBlock, ...]

    def scores(self, *, risk_free_rate: Decimal) -> Scores:
        pnls = [t.net_pnl for t in self.closed]
        found = expectancy([Trade(t.net_pnl, t.equity_at_entry) for t in self.closed])
        return Scores(
            trades=len(self.closed),
            sharpe=sharpe(self.curve, risk_free_rate=risk_free_rate),
            max_drawdown=max_drawdown(self.curve),
            profit_factor=profit_factor(pnls),
            expectancy_rupees=None if found is None else found.rupees,
            expectancy_fraction=None if found is None else found.fraction,
            ending_equity=self.curve[-1],
            open_positions=len(self.open),
        )


def read_account(conn: psycopg.Connection, backtest_id: uuid.UUID) -> Account:
    """The backtest ``backtest_id`` as the Ledger holds it."""
    runs = queries.runs(conn, mode="backtest", backtest_id=backtest_id)
    if not runs:
        raise ValueError(f"no backtest {backtest_id} in the Ledger")
    curve = []
    for ran in runs:
        if ran.equity is None:
            raise ValueError(f"the Run on {ran.as_of} records no Equity; backtest it again")
        curve.append(ran.equity)
    flows: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for fill in queries.fills(conn, mode="backtest", backtest_id=backtest_id):
        flows[fill.position_id] += fill.cash_flow
    for event in queries.position_events(conn, mode="backtest", backtest_id=backtest_id):
        if event.cash_in_lieu is not None:
            flows[event.position_id] += event.cash_in_lieu
    passes: dict[uuid.UUID, dict[int, queries.LedgerPass]] = {}
    blocks: list[queries.LedgerBlock] = []
    for ran in runs:
        blocks += queries.blocks(conn, ran.id)
    closed, still_open = [], []
    last = runs[-1].as_of
    for position in queries.positions(conn, mode="backtest", backtest_id=backtest_id):
        if position.exit_reason is None or position.exit_price is None:
            still_open.append(_marked(conn, position, last))
            continue
        if position.run_id not in passes:
            passes[position.run_id] = {p.rank: p for p in queries.passes(conn, position.run_id)}
        granted = passes[position.run_id][position.rank]
        closed.append(
            ClosedTrade(
                symbol=position.symbol,
                instrument=position.instrument,
                opened_on=position.opened_on,
                closed_on=position.as_of,
                shares=position.shares,
                entry=position.entry,
                stop=position.stop,
                target=position.target,
                exit_price=position.exit_price,
                exit_reason=position.exit_reason,
                net_pnl=flows[position.id],
                equity_at_entry=granted.facts["equity"],
            )
        )
    closed.sort(key=lambda t: (t.closed_on, t.opened_on, t.symbol))
    return Account(
        tuple(r.as_of for r in runs), tuple(curve), tuple(closed), tuple(still_open), tuple(blocks)
    )


def _marked(conn: psycopg.Connection, position: queries.LedgerPosition, day: date) -> OpenPosition:
    bars = queries.adjusted_daily_candles(
        conn, position.instrument, start=day - MARK_LOOKBACK, end=day, as_of=day
    )
    closes = [b.close for b in bars if b.close is not None]
    if not closes:
        raise ValueError(f"{position.symbol} has no close in the month to {day} to mark it at")
    return OpenPosition(
        symbol=position.symbol,
        instrument=position.instrument,
        opened_on=position.opened_on,
        shares=position.shares,
        entry=position.entry,
        stop=position.stop,
        target=position.target,
        close=closes[-1],
    )


@dataclass(frozen=True)
class Variant:
    """The cell backtested again with the impact c at another value."""

    impact: Decimal
    scores: Scores


@dataclass(frozen=True)
class BacktestReport:
    strategy: queries.LedgerStrategy
    parameters: Mapping[str, str]
    rule_version: str
    start: date
    end: date
    starting_equity: Decimal
    costs: CostModel
    risk_free_rate: Decimal
    halts: HaltLimits
    account: Account
    variants: tuple[Variant, ...]
    contract_notes: int

    @property
    def scores(self) -> Scores:
        return self.account.scores(risk_free_rate=self.risk_free_rate)

    @property
    def halt_days(self) -> list[HaltDay]:
        return halt_days(
            self.account.days,
            self.account.curve,
            daily_loss=self.halts.daily_loss,
            drawdown=self.halts.drawdown,
        )

    def as_json(self) -> dict[str, Any]:
        account = self.account
        by_limit = Counter(b.limit for b in account.blocks)
        halts = self.halt_days
        return {
            "strategy": {
                "family": self.strategy.family,
                "name": self.strategy.name,
                "code_revision": self.strategy.code_revision,
                "parameters": dict(sorted(self.parameters.items())),
            },
            "universe_rule_version": self.rule_version,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "trading_days": len(account.days),
            "halt_would_have_fired": {
                "daily_loss_limit": _text(self.halts.daily_loss, FRACTION),
                "drawdown_limit": _text(self.halts.drawdown, FRACTION),
                "days": [
                    {
                        "day": h.day.isoformat(),
                        "limits": list(h.limits),
                        "daily_loss": _text(h.daily_loss, FRACTION),
                        "drawdown": _text(h.drawdown, FRACTION),
                    }
                    for h in halts
                ],
            },
            "metrics": self.scores.as_json(),
            "starting_equity": _text(self.starting_equity, RUPEE),
            "risk_free_rate": _text(self.risk_free_rate, FRACTION),
            "costs": {
                "rates_as_of": None
                if self.costs.rates_as_of is None
                else self.costs.rates_as_of.isoformat(),
                "safety_margin": str(self.costs.slippage.margin),
                "impact": str(self.costs.slippage.impact),
            },
            "cost_sensitivity": [
                {"impact": str(v.impact), **v.scores.as_json()} for v in self.variants
            ],
            "gapped_stops": [
                {
                    "symbol": t.symbol,
                    "opened_on": t.opened_on.isoformat(),
                    "closed_on": t.closed_on.isoformat(),
                    "shares": t.shares,
                    "entry": _text(t.entry, RUPEE),
                    "stop": _text(t.stop, RUPEE),
                    "exit_price": _text(t.exit_price, RUPEE),
                    "planned_risk": _text(t.planned_risk, RUPEE),
                    "realised_loss": _text(-t.net_pnl, RUPEE),
                }
                for t in account.closed
                if t.gapped
            ],
            "candidates_beyond_passes": by_limit["max_new_entries"],
            "unclassified_blocks": sum(
                b.limit == "max_sector" and b.sector == UNCLASSIFIED for b in account.blocks
            ),
            "blocks_by_limit": dict(sorted(by_limit.items())),
            "contract_notes": "none available"
            if self.contract_notes == 0
            else f"{self.contract_notes} checked against the rates on their dates",
            "closed_trades": [
                {
                    "symbol": t.symbol,
                    "opened_on": t.opened_on.isoformat(),
                    "closed_on": t.closed_on.isoformat(),
                    "shares": t.shares,
                    "entry": _text(t.entry, RUPEE),
                    "stop": _text(t.stop, RUPEE),
                    "target": _text(t.target, RUPEE),
                    "exit_price": _text(t.exit_price, RUPEE),
                    "exit_reason": t.exit_reason,
                    "net_pnl": _text(t.net_pnl, RUPEE),
                    "equity_at_entry": _text(t.equity_at_entry, RUPEE),
                }
                for t in account.closed
            ],
            "open_positions": [
                {
                    "symbol": p.symbol,
                    "opened_on": p.opened_on.isoformat(),
                    "shares": p.shares,
                    "entry": _text(p.entry, RUPEE),
                    "stop": _text(p.stop, RUPEE),
                    "target": _text(p.target, RUPEE),
                    "close": _text(p.close, RUPEE),
                    "value": _text(p.value, RUPEE),
                    "unrealised": _text(p.unrealised, RUPEE),
                }
                for p in account.open
            ],
        }

    def text(self) -> str:
        return json.dumps(self.as_json(), indent=2) + "\n"


@dataclass(frozen=True)
class ReportResult:
    report: BacktestReport
    # The backtest of the cell, then each variant's, by impact.
    backtest_id: uuid.UUID
    variant_ids: Mapping[str, uuid.UUID]
    # Wall-clock seconds of the cell's backtest, then each variant's.
    seconds: float
    variant_seconds: Mapping[str, float]

    def summary(self) -> dict[str, Any]:
        scores = self.report.scores.as_json()
        return {
            "backtest_id": str(self.backtest_id),
            "variant_ids": {k: str(v) for k, v in self.variant_ids.items()},
            "seconds": round(self.seconds, 1),
            "variant_seconds": {k: round(v, 1) for k, v in self.variant_seconds.items()},
            "trades": scores["trades"],
            "sharpe": scores["sharpe"],
            "max_drawdown": scores["max_drawdown"],
            "profit_factor": scores["profit_factor"],
            "halt_days": len(self.report.halt_days),
        }


def report_backtest(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    start: date,
    end: date,
    calendar: TradingCalendar,
    rule_version: str,
    limits: Limits,
    tick_sizes: TickSizes,
    costs: CostModel,
    risk_free_rate: Decimal,
    halts: HaltLimits,
    contract_notes: int,
    starting_equity: Decimal = STARTING_EQUITY,
) -> ReportResult:
    """Backtest the cell from ``start`` to ``end``, then with c at zero and at twice
    ``costs``' impact, and report all three from the Ledger."""

    def timed(model: CostModel) -> tuple[uuid.UUID, float]:
        began = time.monotonic()
        results = backtest(
            conn,
            strategy,
            start=start,
            end=end,
            calendar=calendar,
            rule_version=rule_version,
            limits=limits,
            tick_sizes=tick_sizes,
            costs=model,
            starting_equity=starting_equity,
        )
        found = results[0].backtest_id
        assert found is not None
        return found, time.monotonic() - began

    backtest_id, seconds = timed(costs)
    impact = costs.slippage.impact
    variant_ids: dict[str, uuid.UUID] = {}
    variant_seconds: dict[str, float] = {}
    variants = []
    for value in (Decimal(0), impact * 2):
        model = replace(costs, slippage=replace(costs.slippage, impact=value))
        variant_ids[str(value)], variant_seconds[str(value)] = timed(model)
        account = read_account(conn, variant_ids[str(value)])
        variants.append(Variant(value, account.scores(risk_free_rate=risk_free_rate)))
    account = read_account(conn, backtest_id)
    (ran, *_) = queries.runs(conn, mode="backtest", backtest_id=backtest_id)
    report = BacktestReport(
        strategy=ran.strategy,
        parameters=ran.parameters,
        rule_version=ran.universe_rule_version,
        start=start,
        end=end,
        starting_equity=starting_equity,
        costs=costs,
        risk_free_rate=risk_free_rate,
        halts=halts,
        account=account,
        variants=tuple(variants),
        contract_notes=contract_notes,
    )
    return ReportResult(report, backtest_id, variant_ids, seconds, variant_seconds)


def _text(value: Decimal | None, places: Decimal) -> str | None:
    return None if value is None else str(value.quantize(places))
