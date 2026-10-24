"""The engine: one Run of a Strategy as of a day, written to the Ledger (ADR 0001, 0002, 0019).

A Run reads the month's Universe and the Adjusted Series as of its day through
``tradeline.queries``, asks the Strategy for its ranked Candidates and a Plan
for each, and puts every Plan, in Plan order, to the Risk Engine
(``tradeline.risk``), which prices it on the tick in force, sizes it, and
Passes or Blocks it. It writes the Strategy version, the Run, the Candidates,
the Plans, the Passes and Blocks, and a Verdict on each Candidate (BUY for a
Pass, SKIP for a Block) to the Ledger in one transaction, every row tagged
with the Engine Mode (and, in backtest mode, a backtest id). The mode decides
the clock and the data's origin, never what the Strategy or the Risk Engine
does: the code below has no branch on it beyond the backtest id the Ledger
requires.

In backtest mode the clock is simulated: ``SimulatedClock`` steps one trading
day at a time over the Trading Calendar, and each step is one Run as of that
day. The data source, ``StoredMarketData``, is bound to the Run's As-Of Date and
refuses any read past it. What the Risk Engine reads about each Plan's
Instrument comes through it too, none of it after the As-Of Date: the close
NSE fixes the month's tick by (the last close of the month before), and the
20-day average daily traded value. The sector is the load in force, or the
earliest load for a day before it (``queries.sectors_for_run``).

Not yet here: Orders, Fills and Positions (#77), so every Run sizes against
its starting Equity with nothing held; and Protocols (#81), so a Strategy
version is recorded without one and the starting Equity is a constant.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

import psycopg

from tradeline import queries
from tradeline.adjust import AdjustedBar
from tradeline.dates import month_of
from tradeline.revision import code_revision
from tradeline.risk import Decision, Limits, Pass, Portfolio, Proposal, assess
from tradeline.strategy import Candidate, Member, Plan, Strategy
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)

EngineMode = Literal["backtest", "paper", "live"]

# Equity at the start of a backtest, until the Protocol states it (#81).
STARTING_EQUITY = Decimal("500000")
# The trading days the Participation Cap averages traded value over.
TRADED_VALUE_DAYS = 20
# How far before the month the close that fixes the tick is looked for.
REFERENCE_LOOKBACK = timedelta(days=31)


class SimulatedClock:
    """Backtest time: the Trading Calendar's open days from ``start`` to ``end``."""

    def __init__(self, calendar: TradingCalendar, *, start: date, end: date) -> None:
        if end < start:
            raise ValueError(f"range end {end} is before start {start}")
        self._calendar = calendar
        self._start, self._end = start, end

    def days(self) -> Iterator[date]:
        return self._calendar.trading_days(self._start, self._end)


class StoredMarketData:
    """The Adjusted Series as it stood on ``as_of``, read from stored end-of-day rows."""

    def __init__(self, conn: psycopg.Connection, calendar: TradingCalendar, *, as_of: date) -> None:
        self._conn = conn
        self._calendar = calendar
        self._as_of = as_of

    @property
    def as_of(self) -> date:
        return self._as_of

    def bars(self, instrument: str, *, start: date, end: date) -> list[AdjustedBar]:
        """Refused when ``end`` is after the As-Of Date: no Run may see a later bar."""
        return queries.adjusted_daily_candles(
            self._conn, instrument, start=start, end=end, as_of=self._as_of
        )

    def last_bars(self, instrument: str, count: int) -> list[AdjustedBar]:
        return self.bars(instrument, start=self._last_days(count)[0], end=self._as_of)

    def average_traded_value(self, instrument: str, days: int) -> Decimal:
        """The rupees traded a day over the last ``days`` trading days up to ``as_of``, a
        day without trading counting as none."""
        start = self._last_days(days)[0]
        rows = queries.end_of_day(
            self._conn, instrument, start=start, end=self._as_of, as_of=self._as_of
        )
        return sum((r.traded_value for r in rows), Decimal(0)) / days

    def reference_close(self, instrument: str) -> Decimal:
        """The close NSE fixes a security's tick by for the month (NSE/CMTR/62174): the latest
        close on or before the last day of the month before; for a security first traded
        this month, its first close."""
        month = month_of(self._as_of)
        before = self._closes(instrument, start=month - REFERENCE_LOOKBACK, end=month)
        if before:
            return before[-1]
        this_month = self._closes(instrument, start=month, end=self._as_of + timedelta(days=1))
        if not this_month:
            raise ValueError(f"{instrument} has no close by {self._as_of} to fix its tick by")
        return this_month[0]

    def _closes(self, instrument: str, *, start: date, end: date) -> list[Decimal]:
        """Closes from ``start`` to the day before ``end``, oldest first."""
        rows = queries.end_of_day(
            self._conn, instrument, start=start, end=end - timedelta(days=1), as_of=self._as_of
        )
        return [r.close for r in rows if r.close is not None]

    def _last_days(self, count: int) -> list[date]:
        """The last ``count`` trading days up to and including ``as_of``."""
        # Twice the span in calendar days covers ``count`` trading days at any holiday density.
        days = list(
            self._calendar.trading_days(self._as_of - timedelta(days=2 * count + 14), self._as_of)
        )
        if len(days) < count:
            raise ValueError(
                f"the Trading Calendar has fewer than {count} days before {self._as_of}"
            )
        return days[-count:]


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID
    mode: EngineMode
    backtest_id: uuid.UUID | None
    as_of: date
    universe_size: int
    candidates: int
    plans: int
    passes: int
    blocks: int

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "mode": self.mode,
            "backtest_id": None if self.backtest_id is None else str(self.backtest_id),
            "as_of": self.as_of.isoformat(),
            "universe_size": self.universe_size,
            "candidates": self.candidates,
            "plans": self.plans,
            "passes": self.passes,
            "blocks": self.blocks,
        }


def run(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    mode: EngineMode,
    as_of: date,
    calendar: TradingCalendar,
    rule_version: str,
    limits: Limits,
    tick_sizes: TickSizes,
    equity: Decimal,
    backtest_id: uuid.UUID | None = None,
) -> RunResult:
    """One Run as of ``as_of`` over the month's Universe under ``rule_version``, its Plans
    checked against ``limits`` at ``equity`` with nothing held.

    A backtest id is required in backtest mode and refused in any other.
    """
    if (mode == "backtest") != (backtest_id is not None):
        raise ValueError("a backtest id is required in backtest mode and only there")
    if as_of not in set(calendar.trading_days(as_of, as_of)):
        raise ValueError(f"{as_of} is not a trading day on the Trading Calendar")
    month = month_of(as_of)
    universe = queries.universe_membership(conn, month, as_of=as_of, rule_version=rule_version)
    if not universe:
        raise ValueError(
            f"no Universe for {month:%Y-%m} under rule version {rule_version[:12]} as of {as_of};"
            " run `tradeline universe` for the month first"
        )
    members = [Member(m.instrument or m.symbol, m.symbol) for m in universe]
    data = StoredMarketData(conn, calendar, as_of=as_of)
    candidates = strategy.screen(members, data, as_of)
    plans = [strategy.plan(c) for c in candidates]
    run_id = uuid.uuid4()
    proposals = _proposals(conn, plans, data, tick_sizes, as_of=as_of)
    decisions = assess(proposals, portfolio=Portfolio(equity), limits=limits, run_id=run_id)
    with conn.transaction():
        strategy_id = _strategy_row(conn, strategy, mode=mode, backtest_id=backtest_id)
        _insert_run(
            conn,
            strategy,
            run_id=run_id,
            mode=mode,
            backtest_id=backtest_id,
            strategy_id=strategy_id,
            as_of=as_of,
            rule_version=rule_version,
            universe_size=len(members),
        )
        _insert_candidates(conn, candidates, mode=mode, backtest_id=backtest_id, run_id=run_id)
        _insert_plans(conn, plans, mode=mode, backtest_id=backtest_id, run_id=run_id)
        _insert_decisions(
            conn, decisions, proposals, equity=equity, mode=mode, backtest_id=backtest_id
        )
    conn.commit()
    passed = sum(isinstance(d, Pass) for d in decisions)
    result = RunResult(
        run_id,
        mode,
        backtest_id,
        as_of,
        len(members),
        len(candidates),
        len(plans),
        passed,
        len(decisions) - passed,
    )
    log.info("run written to the ledger", extra=result.summary())
    return result


def backtest(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    start: date,
    end: date,
    calendar: TradingCalendar,
    rule_version: str,
    limits: Limits,
    tick_sizes: TickSizes,
    equity: Decimal = STARTING_EQUITY,
) -> list[RunResult]:
    """A backtest from ``start`` to ``end``: one Run per day of the simulated clock, all
    under one backtest id."""
    backtest_id = uuid.uuid4()
    results = [
        run(
            conn,
            strategy,
            mode="backtest",
            as_of=day,
            calendar=calendar,
            rule_version=rule_version,
            limits=limits,
            tick_sizes=tick_sizes,
            equity=equity,
            backtest_id=backtest_id,
        )
        for day in SimulatedClock(calendar, start=start, end=end).days()
    ]
    if not results:
        raise ValueError(f"no trading day from {start} to {end} on the Trading Calendar")
    return results


def _proposals(
    conn: psycopg.Connection,
    plans: Sequence[Plan],
    data: StoredMarketData,
    tick_sizes: TickSizes,
    *,
    as_of: date,
) -> list[Proposal]:
    """Each Plan with what the Limits read about its Instrument as of the Run."""
    found = queries.sectors_for_run(conn, sorted({p.symbol for p in plans}), as_of=as_of)
    sectors = {s.symbol: s.sector for s in found}
    return [
        Proposal(
            plan,
            tick=tick_sizes.tick(as_of, reference_close=data.reference_close(plan.instrument)),
            sector=sectors.get(plan.symbol),
            average_traded_value=data.average_traded_value(plan.instrument, TRADED_VALUE_DAYS),
        )
        for plan in plans
    ]


def _strategy_row(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
) -> uuid.UUID:
    revision = code_revision(type(strategy).__module__)
    key = {
        "mode": mode,
        "backtest_id": backtest_id,
        "family": strategy.family,
        "name": strategy.name,
        "code_revision": revision,
        "ranking": strategy.ranking,
    }
    conn.execute(
        "INSERT INTO strategies (mode, backtest_id, family, name, code_revision, ranking)"
        " VALUES (%(mode)s, %(backtest_id)s, %(family)s, %(name)s, %(code_revision)s,"
        "  %(ranking)s)"
        " ON CONFLICT DO NOTHING",
        key,
    )
    row = conn.execute(
        "SELECT id FROM strategies WHERE mode = %(mode)s"
        " AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s AND name = %(name)s"
        " AND code_revision = %(code_revision)s AND protocol IS NULL",
        key,
    ).fetchone()
    assert row is not None
    found: uuid.UUID = row[0]
    return found


def _insert_run(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    run_id: uuid.UUID,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    strategy_id: uuid.UUID,
    as_of: date,
    rule_version: str,
    universe_size: int,
) -> None:
    conn.execute(
        "INSERT INTO runs (id, mode, backtest_id, strategy_id, as_of, universe_month,"
        " universe_rule_version, universe_size, parameters)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            mode,
            backtest_id,
            strategy_id,
            as_of,
            month_of(as_of),
            rule_version,
            universe_size,
            json.dumps(dict(strategy.parameters()), sort_keys=True),
        ),
    )


def _facts(values: Any) -> str:
    return json.dumps({name: str(value) for name, value in values.items()}, sort_keys=True)


def _insert_candidates(
    conn: psycopg.Connection,
    candidates: Sequence[Candidate],
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    run_id: uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO candidates (mode, backtest_id, run_id, rank, instrument, symbol, score,"
            " facts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mode,
                    backtest_id,
                    run_id,
                    c.rank,
                    c.instrument,
                    c.symbol,
                    c.score,
                    _facts(c.facts),
                )
                for c in candidates
            ],
        )


def _insert_plans(
    conn: psycopg.Connection,
    plans: Sequence[Plan],
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    run_id: uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO plans (mode, backtest_id, run_id, rank, instrument, symbol, entry, stop,"
            " target, time_stop_days, facts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mode,
                    backtest_id,
                    run_id,
                    p.rank,
                    p.instrument,
                    p.symbol,
                    p.entry,
                    p.stop,
                    p.target,
                    p.time_stop_days,
                    _facts(p.facts),
                )
                for p in plans
            ],
        )


def _insert_decisions(
    conn: psycopg.Connection,
    decisions: Sequence[Decision],
    proposals: Sequence[Proposal],
    *,
    equity: Decimal,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
) -> None:
    """Each Pass or Block, with what it read, and the Verdict it makes."""
    with conn.cursor() as cursor:
        for decision, proposal in zip(decisions, proposals, strict=True):
            plan = decision.plan
            facts = _facts(
                {
                    "equity": equity,
                    "tick": proposal.tick,
                    "average_traded_value": proposal.average_traded_value,
                    **{f"allowed_{name}": shares for name, shares in decision.allowed.items()},
                }
            )
            priced = (
                plan.entry,
                plan.stop,
                plan.target,
                plan.time_stop_days,
                plan.shares,
                decision.sector,
                facts,
            )
            if isinstance(decision, Pass):
                cursor.execute(
                    "INSERT INTO passes (mode, backtest_id, run_id, rank, plan_hash, entry, stop,"
                    " target, time_stop_days, shares, sector, facts)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (mode, backtest_id, decision.run_id, plan.rank, decision.plan_hash, *priced),
                )
                verdict, reason = "BUY", "pass"
            else:
                cursor.execute(
                    "INSERT INTO blocks (mode, backtest_id, run_id, rank, limit_name, entry, stop,"
                    " target, time_stop_days, shares, sector, facts)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (mode, backtest_id, decision.run_id, plan.rank, decision.limit, *priced),
                )
                verdict, reason = "SKIP", decision.limit
            cursor.execute(
                "INSERT INTO verdicts (mode, backtest_id, run_id, rank, verdict, reason)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (mode, backtest_id, decision.run_id, plan.rank, verdict, reason),
            )
