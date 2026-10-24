"""The single-run report: one backtest scored the way the Backtest Gate will score it.

``report_backtest`` backtests one cell (one Strategy at fixed parameters) over a
range, then again with the Slippage impact c at zero and at twice its value, and
reads all three back from the Ledger (``tradeline.queries``); the report is that
reading and nothing else. Its metrics are ``tradeline.metrics`` over the daily
marked Equity curve (the starting Equity, then each Run's Equity) and the closed
trades.

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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.costs import CostModel
from tradeline.engine import backtest
from tradeline.metrics import (
    HaltDay,
    HaltLimits,
    PositionResult,
    expectancy,
    halt_days,
    max_drawdown,
    profit_factor,
    sharpe,
)
from tradeline.paper_broker import ExitReason
from tradeline.risk import UNCLASSIFIED
from tradeline.strategy import Strategy
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar

RUPEE = Decimal("0.01")
FRACTION = Decimal("0.000001")
RATIO = Decimal("0.0001")
# How far back the last day's close of an open Position is looked for.
MARK_LOOKBACK = timedelta(days=31)


@dataclass(frozen=True)
class ClosedPosition:
    symbol: str
    instrument: str
    opened_on: date
    closed_on: date
    shares: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit_price: Decimal
    exit_reason: ExitReason
    net_pnl: Decimal
    equity_at_entry: Decimal
    # The sector its Pass was held to; ``unclassified`` when none was recorded.
    sector: str = UNCLASSIFIED

    @property
    def gapped(self) -> bool:
        """A Gapped Stop: the stop's exit filled under the stop."""
        return self.exit_reason == "stop" and self.exit_price < self.stop

    @property
    def planned_risk(self) -> Decimal:
        return self.shares * (self.entry - self.stop)

    def as_json(self) -> dict[str, Any]:
        """The closed Position as every report lists it."""
        return {
            "symbol": self.symbol,
            "opened_on": self.opened_on.isoformat(),
            "closed_on": self.closed_on.isoformat(),
            "shares": self.shares,
            "entry": rounded(self.entry, RUPEE),
            "stop": rounded(self.stop, RUPEE),
            "target": rounded(self.target, RUPEE),
            "exit_price": rounded(self.exit_price, RUPEE),
            "exit_reason": self.exit_reason,
            "net_pnl": rounded(self.net_pnl, RUPEE),
            "equity_at_entry": rounded(self.equity_at_entry, RUPEE),
            "sector": self.sector,
        }


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

    def as_json(self) -> dict[str, Any]:
        """The open Position as every report lists it, marked at the last day's close."""
        return {
            "symbol": self.symbol,
            "opened_on": self.opened_on.isoformat(),
            "shares": self.shares,
            "entry": rounded(self.entry, RUPEE),
            "stop": rounded(self.stop, RUPEE),
            "target": rounded(self.target, RUPEE),
            "close": rounded(self.close, RUPEE),
            "value": rounded(self.value, RUPEE),
            "unrealised": rounded(self.unrealised, RUPEE),
        }


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
            "sharpe": rounded(self.sharpe, RATIO),
            "max_drawdown": rounded(self.max_drawdown, FRACTION),
            "profit_factor": rounded(self.profit_factor, RATIO),
            "expectancy": {
                "rupees": rounded(self.expectancy_rupees, RUPEE),
                "fraction_of_equity": rounded(self.expectancy_fraction, FRACTION),
            },
            "ending_equity": rounded(self.ending_equity, RUPEE),
            "open_positions": self.open_positions,
        }


@dataclass(frozen=True)
class Account:
    """One backtest read back from the Ledger: its daily curve, its closed trades, its open
    Positions marked at the last close, its Passes and its Blocks. The curve starts at the
    account's starting Equity, the day before ``days[0]``, then holds each day's Equity,
    so the first day's return counts."""

    days: tuple[date, ...]
    curve: tuple[Decimal, ...]
    closed: tuple[ClosedPosition, ...]
    open: tuple[OpenPosition, ...]
    blocks: tuple[queries.LedgerBlock, ...]
    passes: tuple[queries.LedgerPass, ...] = ()

    def limits_json(self) -> dict[str, Any]:
        """Where the Limits bound: the Candidates beyond the Passes (Blocked by the entry
        cap), the Passes the Participation Cap sized down and the Plans it Blocked, the
        Blocks caused by Unclassified and the trades held in it, and every Block by Limit."""
        by_limit = Counter(b.limit for b in self.blocks)
        return {
            "candidates_beyond_passes": by_limit["max_new_entries"],
            "participation_cap": {
                "passes_sized_down": sum(_capped(p) for p in self.passes),
                "blocks": by_limit["participation_cap"],
            },
            "unclassified": {
                "blocks": sum(
                    b.limit == "max_sector" and b.sector == UNCLASSIFIED for b in self.blocks
                ),
                "trades": sum(t.sector == UNCLASSIFIED for t in self.closed),
            },
            "blocks_by_limit": dict(sorted(by_limit.items())),
        }

    def gapped_stops_json(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": t.symbol,
                "opened_on": t.opened_on.isoformat(),
                "closed_on": t.closed_on.isoformat(),
                "shares": t.shares,
                "entry": rounded(t.entry, RUPEE),
                "stop": rounded(t.stop, RUPEE),
                "exit_price": rounded(t.exit_price, RUPEE),
                "planned_risk": rounded(t.planned_risk, RUPEE),
                "realised_loss": rounded(-t.net_pnl, RUPEE),
            }
            for t in self.closed
            if t.gapped
        ]

    def halt_days(self, halts: HaltLimits) -> list[HaltDay]:
        return halt_days(
            self.days, self.curve, daily_loss=halts.daily_loss, drawdown=halts.drawdown
        )

    def scores(self, *, risk_free_rate: Decimal) -> Scores:
        pnls = [t.net_pnl for t in self.closed]
        found = expectancy([PositionResult(t.net_pnl, t.equity_at_entry) for t in self.closed])
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
    protocol = runs[0].strategy.protocol
    frozen = None if protocol is None else queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(f"backtest {backtest_id} names no frozen Protocol to start it from")
    curve = [frozen.protocol.starting_equity]
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
        passes[ran.id] = {p.rank: p for p in queries.passes(conn, ran.id)}
    closed, still_open = [], []
    last = runs[-1].as_of
    for position in queries.positions(conn, mode="backtest", backtest_id=backtest_id):
        if position.exit_reason is None or position.exit_price is None:
            still_open.append(_marked(conn, position, last))
            continue
        granted = passes[position.run_id][position.rank]
        closed.append(
            ClosedPosition(
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
                sector=granted.sector,
            )
        )
    closed.sort(key=lambda t: (t.closed_on, t.opened_on, t.symbol))
    return Account(
        tuple(r.as_of for r in runs),
        tuple(curve),
        tuple(closed),
        tuple(still_open),
        tuple(blocks),
        tuple(p for ranked in passes.values() for p in ranked.values()),
    )


def _capped(granted: queries.LedgerPass) -> bool:
    """The Participation Cap sized the Pass down: it allowed fewer shares than its stop's
    risk did, and the Pass took the cap's."""
    facts = granted.facts
    cap = facts.get("allowed_participation_cap")
    return (
        cap is not None and cap == granted.shares and cap < facts.get("allowed_risk_per_trade", cap)
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
        return self.account.halt_days(self.halts)

    def as_json(self) -> dict[str, Any]:
        account = self.account
        by_limit = Counter(b.limit for b in account.blocks)
        return {
            "protocol": self.strategy.protocol,
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
                "daily_loss_limit": rounded(self.halts.daily_loss, FRACTION),
                "drawdown_limit": rounded(self.halts.drawdown, FRACTION),
                "days": halt_days_json(self.halt_days),
            },
            "metrics": self.scores.as_json(),
            "starting_equity": rounded(self.starting_equity, RUPEE),
            "risk_free_rate": rounded(self.risk_free_rate, FRACTION),
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
            "gapped_stops": account.gapped_stops_json(),
            "candidates_beyond_passes": by_limit["max_new_entries"],
            "unclassified_blocks": sum(
                b.limit == "max_sector" and b.sector == UNCLASSIFIED for b in account.blocks
            ),
            "blocks_by_limit": dict(sorted(by_limit.items())),
            "contract_notes": "none available"
            if self.contract_notes == 0
            else f"{self.contract_notes} checked against the rates on their dates",
            "closed_positions": [t.as_json() for t in account.closed],
            "open_positions": [p.as_json() for p in account.open],
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
    protocol: str | None,
    start: date,
    end: date,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    contract_notes: int,
) -> ReportResult:
    """Backtest the cell from ``start`` to ``end`` under the frozen Protocol ``protocol``,
    then with its Slippage impact c at zero and at twice its value, and report all three from
    the Ledger, scored at the Protocol's risk-free rate, the Halt days at its Halt fractions."""

    def timed(impact: Decimal | None) -> tuple[uuid.UUID, float]:
        began = time.monotonic()
        results = backtest(
            conn,
            strategy,
            protocol=protocol,
            start=start,
            end=end,
            calendar=calendar,
            tick_sizes=tick_sizes,
            impact=impact,
        )
        found = results[0].backtest_id
        assert found is not None
        return found, time.monotonic() - began

    backtest_id, seconds = timed(None)
    (ran, *_) = queries.runs(conn, mode="backtest", backtest_id=backtest_id)
    assert ran.strategy.protocol is not None
    frozen = queries.protocol(conn, ran.strategy.protocol)
    assert frozen is not None
    terms = frozen.protocol
    variant_ids: dict[str, uuid.UUID] = {}
    variant_seconds: dict[str, float] = {}
    variants = []
    for value in (Decimal(0), terms.slippage.impact * 2):
        variant_ids[str(value)], variant_seconds[str(value)] = timed(value)
        (first, *_) = queries.runs(conn, mode="backtest", backtest_id=variant_ids[str(value)])
        assert first.impact is not None
        account = read_account(conn, variant_ids[str(value)])
        variants.append(Variant(first.impact, account.scores(risk_free_rate=terms.risk_free_rate)))
    report = BacktestReport(
        strategy=ran.strategy,
        parameters=ran.parameters,
        rule_version=ran.universe_rule_version,
        start=start,
        end=end,
        starting_equity=terms.starting_equity,
        costs=terms.cost_model(),
        risk_free_rate=terms.risk_free_rate,
        halts=terms.halts,
        account=read_account(conn, backtest_id),
        variants=tuple(variants),
        contract_notes=contract_notes,
    )
    return ReportResult(report, backtest_id, variant_ids, seconds, variant_seconds)


def halt_days_json(days: list[HaltDay]) -> list[dict[str, Any]]:
    return [
        {
            "day": h.day.isoformat(),
            "limits": list(h.limits),
            "daily_loss": rounded(h.daily_loss, FRACTION),
            "drawdown": rounded(h.drawdown, FRACTION),
        }
        for h in days
    ]


def rounded(value: Decimal | None, places: Decimal) -> str | None:
    """``value`` as the reports print it: quantized to ``places`` (half-even), an unbounded
    value (a profit factor with no losing trade) as ``∞``, None as is."""
    if value is None:
        return None
    if value.is_infinite():
        return "∞" if value > 0 else "-∞"
    return str(value.quantize(places))
