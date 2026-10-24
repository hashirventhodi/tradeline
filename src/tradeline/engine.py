"""The engine: one day of a Strategy's account, written to the Ledger (ADR 0001, 0002, 0019).

A day is its session, then its Run, then the Orders for the next session, in
one transaction. The session fills the Orders the day before placed against
the day's bar through the Paper Broker's fill model (``tradeline.paper_broker``,
ADR 0003): entries and exits at the open, each Position's resting target
through the day. Each Fill opens or closes a Position; Orders, Fills and
Positions are event rows, ordered within the account by day and a sequence
number. After the Run each open Position gets its one sell Order for the next
session: its target re-placed, or, when the completed bar reached the stop or
the time stop came due (counted in trading days from the Fill), the target
cancelled and an exit placed at the close less the protective band; an exit
the open did not fill is sent again. Each Pass becomes an entry Order. A
Candidate whose Instrument is already held is not put to the Risk Engine: its
Verdict is SKIP, reason ``held``, since a Position is the one holding in its
Instrument.

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
refuses any read past it. A backtest loads each Instrument's rows once
(``SeriesCache``) and re-adjusts them only when an action's Ex-Date passes, the
only day its Adjusted Series changes; each read is still as of the Run's day.
What the Risk Engine reads about each Plan's Instrument comes through it too,
none of it after the As-Of Date: the close NSE fixes the month's tick by (the
last close of the month before), and the 20-day average daily traded value.
The sector is the load in force, or the earliest load for a day before it
(``queries.sectors_for_run``).

In paper mode the clock is the wall clock (``WallClock``, one step: today in
IST or a named day, never a later one) and the data source is
``CollectedMarketData``: the same stored rows ``collect-daily`` loaded, the same
refusal past the As-Of Date, a refusal of a day not yet collected, and the
stale-sector-map alert for an Unclassified Instrument. Its Orders fill the next
day against that day's end-of-day bar through the same fill model; Phase 3
replaces the bar with live quotes. Phase 2's paper Runs are dry runs: every row
is marked ``dry_run``, an account of its own that the Paper Gate never counts
(``queries.paper_gate_trades``). Both modes go through one loop (``_step``),
and the Strategy, the Risk Engine (``RiskEngine``, sizing included), the cost
model and the fill model (``FillModel``) are passed through it unchanged.

Every Fill carries its charges and Slippage from the cost model
(``tradeline.costs``), itemised. The account's cash is its starting Equity
plus what its Fills moved (``queries.account_cash_flows``), and Equity is that
cash plus the open Positions at the day's adjusted close, taken after the
session; the Run sizes against it and records both, so Equity compounds.

A split or bonus moves the Adjusted Series, so before the session each held
Position whose Instrument has one ex-dated since the account's last trading
day is restated by the Adjustment Factor: shares divided by it (rounded down,
the fraction paid in cash at the restated previous close), entry multiplied
to the paisa, stop and target multiplied and rounded down to the tick. Equity
is unchanged by the action, and the exits keep their meaning in the new
prices. Orders open for that session in the Instrument are cancelled, reason
``corporate_action`` (NSE purges them on the Ex-Date): a sell is placed again
at the restated prices and shares, an entry is not (the next Run may plan
again).

Protocols are #81's, so a Strategy version is recorded without one, and the
starting Equity, the band and the cost schedule's date are the caller's.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any, Literal, Protocol

import psycopg

from tradeline import queries
from tradeline.adjust import ADJUSTED_KINDS, AdjustedBar
from tradeline.costs import CostModel
from tradeline.dates import IST, month_of
from tradeline.paper_broker import FILL_MODEL, Bar, FillModel, Unfilled
from tradeline.revision import code_revision
from tradeline.risk import (
    RISK_ENGINE,
    Decision,
    Holding,
    Limits,
    Pass,
    Portfolio,
    Proposal,
    RiskEngine,
    verify_pass,
)
from tradeline.strategy import Candidate, Member, Plan, Strategy
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)

EngineMode = Literal["backtest", "paper", "live"]

# Equity at the start of a backtest, until the Protocol states it (#81).
STARTING_EQUITY = Decimal("500000")
# The protective band under the close a stop or time-stop exit is limited at, until the
# Protocol states it (#81).
EXIT_BAND = Decimal("0.05")
# The trading days the Participation Cap averages traded value over.
TRADED_VALUE_DAYS = 20
# How far before a backtest's first day the SeriesCache loads each Instrument's rows.
SERIES_LOOKBACK = timedelta(days=730)
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


def _today() -> date:
    return datetime.now(IST).date()


class WallClock:
    """Paper time: the one day it is now in IST, or ``day`` when one is named (a day missed,
    run late). A day after today is refused: no Run is as of a day yet to come."""

    def __init__(self, *, today: Callable[[], date] = _today, day: date | None = None) -> None:
        self._today = today
        self._day = day

    def days(self) -> Iterator[date]:
        today = self._today()
        day = today if self._day is None else self._day
        if day > today:
            raise ValueError(f"{day} is after today ({today}) on the wall clock")
        yield day


class SeriesCache:
    """Each Instrument's Raw Series over a backtest's span, loaded once from the query layer
    (``queries.adjusted_series_span``) and read as the Adjusted Series as of each day; it
    holds the span's later rows and never returns them. A Symbol is read as what it names
    on the day (``queries.instrument_key``). The span starts ``SERIES_LOOKBACK`` before the
    backtest, enough for any Strategy's lookback; a read from earlier goes to the query
    layer instead."""

    def __init__(self, conn: psycopg.Connection, *, start: date, end: date) -> None:
        self._conn = conn
        self._start, self._end = start - SERIES_LOOKBACK, end
        self._spans: dict[str, queries.AdjustedSeriesSpan] = {}
        # Symbols read as of one day: what each reads as then.
        self._resolved: dict[str, str] = {}
        self._resolved_on: date | None = None

    def bars(self, instrument: str, *, start: date, end: date, as_of: date) -> list[AdjustedBar]:
        if start < self._start or as_of > self._end:
            return queries.adjusted_daily_candles(
                self._conn, instrument, start=start, end=end, as_of=as_of
            )
        owner = self._owner(instrument, as_of)
        span = self._spans.get(owner)
        if span is None:
            span = queries.adjusted_series_span(self._conn, owner, start=self._start, end=self._end)
            self._spans[owner] = span
        return span.bars(start=start, end=end, as_of=as_of)

    def _owner(self, instrument: str, as_of: date) -> str:
        """An Instrument key as it is; a Symbol as what it reads as on ``as_of``."""
        if "@" in instrument:
            return instrument
        if self._resolved_on != as_of:
            self._resolved, self._resolved_on = {}, as_of
        if instrument not in self._resolved:
            self._resolved[instrument] = queries.instrument_key(self._conn, instrument, as_of=as_of)
        return self._resolved[instrument]

    def market_data(
        self, conn: psycopg.Connection, calendar: TradingCalendar, *, as_of: date
    ) -> StoredMarketData:
        """The backtest's data source (a ``MarketSource``): the market as of a day, through
        this cache."""
        return StoredMarketData(conn, calendar, as_of=as_of, cache=self)


class StoredMarketData:
    """The Adjusted Series as it stood on ``as_of``, read from stored end-of-day rows, through
    a backtest's ``SeriesCache`` when it has one."""

    def __init__(
        self,
        conn: psycopg.Connection,
        calendar: TradingCalendar,
        *,
        as_of: date,
        cache: SeriesCache | None = None,
    ) -> None:
        self._conn = conn
        self._calendar = calendar
        self._as_of = as_of
        self._cache = cache
        self._days: list[date] | None = None

    @property
    def as_of(self) -> date:
        return self._as_of

    def bars(self, instrument: str, *, start: date, end: date) -> list[AdjustedBar]:
        """Refused when ``end`` is after the As-Of Date: no Run may see a later bar."""
        if self._cache is not None:
            return self._cache.bars(instrument, start=start, end=end, as_of=self._as_of)
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

    def sectors(self, symbols: Sequence[str]) -> dict[str, str]:
        """The sector of each Symbol that has one as of the Run (``queries.sectors_for_run``);
        a Symbol missing is Unclassified."""
        found = queries.sectors_for_run(self._conn, sorted(set(symbols)), as_of=self._as_of)
        return {s.symbol: s.sector for s in found}

    def _closes(self, instrument: str, *, start: date, end: date) -> list[Decimal]:
        """Closes from ``start`` to the day before ``end``, oldest first."""
        rows = queries.end_of_day(
            self._conn, instrument, start=start, end=end - timedelta(days=1), as_of=self._as_of
        )
        return [r.close for r in rows if r.close is not None]

    def _last_days(self, count: int) -> list[date]:
        """The last ``count`` trading days up to and including ``as_of``."""
        if self._days is None or len(self._days) < count:
            # Twice the span in calendar days covers ``count`` trading days at any holiday
            # density.
            self._days = list(
                self._calendar.trading_days(
                    self._as_of - timedelta(days=2 * count + 14), self._as_of
                )
            )
        days = self._days
        if len(days) < count:
            raise ValueError(
                f"the Trading Calendar has fewer than {count} days before {self._as_of}"
            )
        return days[-count:]


class CollectedMarketData(StoredMarketData):
    """Paper mode's data: what ``collect-daily`` loaded, read as of the wall clock's day, the
    same Adjusted Series and the same refusal of a read past the As-Of Date. A day whose
    end-of-day file is not loaded is refused, so a paper Run never decides on an earlier
    day's data. NSE's sector list is loaded as it stands now, so an Instrument it does not
    classify means the map is stale: a read that finds one Unclassified logs the
    stale-sector-map alert."""

    def __init__(self, conn: psycopg.Connection, calendar: TradingCalendar, *, as_of: date) -> None:
        if as_of not in queries.end_of_day_dates(conn, start=as_of, end=as_of):
            raise ValueError(
                f"no end-of-day data is loaded for {as_of}; run collect-daily for the day first"
            )
        super().__init__(conn, calendar, as_of=as_of)

    def sectors(self, symbols: Sequence[str]) -> dict[str, str]:
        found = super().sectors(symbols)
        unclassified = sorted(set(symbols) - found.keys())
        if unclassified:
            log.warning(
                "stale sector map",
                extra={"as_of": self.as_of.isoformat(), "unclassified": unclassified},
            )
        return found


class MarketSource(Protocol):
    """Where a mode's Run reads the market from, bound to its As-Of Date."""

    def __call__(
        self, conn: psycopg.Connection, calendar: TradingCalendar, *, as_of: date
    ) -> StoredMarketData: ...


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
    # Fills at the day's session, before the Run.
    fills: int = 0
    # Orders the Run placed for the next session.
    orders: int = 0
    # Positions open after the Run.
    open_positions: int = 0
    # The account after the session: Equity is cash plus the open Positions at the close.
    cash: Decimal = Decimal(0)
    equity: Decimal = Decimal(0)
    dry_run: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "mode": self.mode,
            "dry_run": self.dry_run,
            "backtest_id": None if self.backtest_id is None else str(self.backtest_id),
            "as_of": self.as_of.isoformat(),
            "universe_size": self.universe_size,
            "candidates": self.candidates,
            "plans": self.plans,
            "passes": self.passes,
            "blocks": self.blocks,
            "fills": self.fills,
            "orders": self.orders,
            "open_positions": self.open_positions,
            "cash": str(self.cash),
            "equity": str(self.equity),
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
    costs: CostModel,
    starting_equity: Decimal,
    band: Decimal = EXIT_BAND,
    backtest_id: uuid.UUID | None = None,
    dry_run: bool = False,
    data_source: MarketSource = StoredMarketData,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> RunResult:
    """The day ``as_of`` in one account: its held Positions restated for any split or bonus
    ex-dated since its last day, its session, then one Run over the month's Universe under
    ``rule_version``, its Plans checked against ``limits`` at the account's Equity (from
    ``starting_equity``, after ``costs``) with its open Positions held, then the Orders for
    the next session.

    The market is read from ``data_source`` as of the day; the Strategy, ``risk_engine``,
    ``costs`` and ``fill_model`` are used as given, whatever the mode. A backtest id is
    required in backtest mode and refused in any other; ``dry_run`` marks a paper dry-run's
    rows (an account of its own), and only a paper Run can be one.
    """
    if (mode == "backtest") != (backtest_id is not None):
        raise ValueError("a backtest id is required in backtest mode and only there")
    if dry_run and mode != "paper":
        raise ValueError("only a paper Run can be a dry run")
    if as_of not in set(calendar.trading_days(as_of, as_of)):
        raise ValueError(f"{as_of} is not a trading day on the Trading Calendar")
    data = data_source(conn, calendar, as_of=as_of)
    month = month_of(as_of)
    universe = queries.universe_membership(conn, month, as_of=as_of, rule_version=rule_version)
    if not universe:
        raise ValueError(
            f"no Universe for {month:%Y-%m} under rule version {rule_version[:12]} as of {as_of};"
            " run `tradeline universe` for the month first"
        )
    members = [Member(m.instrument or m.symbol, m.symbol) for m in universe]
    account = _Account(
        conn,
        data,
        calendar,
        mode=mode,
        backtest_id=backtest_id,
        dry_run=dry_run,
        costs=costs,
        tick_sizes=tick_sizes,
        fill_model=fill_model,
    )
    tags: queries.Account = {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run}
    next_session = _next_session(calendar, as_of)
    candidates = strategy.screen(members, data, as_of)
    plans = [strategy.plan(c) for c in candidates]
    run_id = uuid.uuid4()
    with conn.transaction():
        fills = account.session()
        held = account.open_positions()
        held_instruments = {p.instrument for p in held}
        # One Position per Instrument: a Candidate already held is skipped, not assessed.
        to_assess = [p for p in plans if p.instrument not in held_instruments]
        sectors = data.sectors([m.symbol for m in members] + [p.symbol for p in held])
        proposals = _proposals(to_assess, data, tick_sizes, sectors, as_of=as_of)
        holdings = _holdings(held, account, sectors)
        cash = starting_equity + queries.account_cash_flows(conn, **tags)
        equity = cash + sum((h.value for h in holdings), Decimal(0))
        portfolio = Portfolio(equity, holdings)
        decisions = risk_engine.assess(proposals, portfolio=portfolio, limits=limits, run_id=run_id)
        strategy_id = _strategy_row(conn, strategy, mode=mode, backtest_id=backtest_id)
        _insert_run(
            conn,
            strategy,
            run_id=run_id,
            **tags,
            strategy_id=strategy_id,
            as_of=as_of,
            rule_version=rule_version,
            universe_size=len(members),
            cash=cash,
            equity=equity,
        )
        _insert_candidates(conn, candidates, **tags, run_id=run_id)
        _insert_plans(conn, plans, **tags, run_id=run_id)
        _insert_decisions(conn, decisions, proposals, equity=equity, **tags)
        _insert_held_verdicts(
            conn, [p for p in plans if p.instrument in held_instruments], **tags, run_id=run_id
        )
        placed = account.place_exits(
            run_id=run_id, session=next_session, band=band, tick_sizes=tick_sizes
        )
        placed += account.place_entries(
            [d for d in decisions if isinstance(d, Pass)], run_id=run_id, session=next_session
        )
        still_open = len(account.open_positions())
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
        fills=fills,
        orders=placed,
        open_positions=still_open,
        cash=cash,
        equity=equity,
        dry_run=dry_run,
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
    costs: CostModel,
    starting_equity: Decimal = STARTING_EQUITY,
    band: Decimal = EXIT_BAND,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> list[RunResult]:
    """A backtest from ``start`` to ``end``: one day per step of the simulated clock, each
    filling the day before's Orders and then Running, all under one backtest id, one account
    starting at ``starting_equity``. ``costs`` should fix its schedule's date
    (``rates_as_of``) so every trade is priced on one schedule."""
    results = _step(
        conn,
        strategy,
        SimulatedClock(calendar, start=start, end=end),
        mode="backtest",
        backtest_id=uuid.uuid4(),
        dry_run=False,
        data_source=SeriesCache(conn, start=start, end=end).market_data,
        calendar=calendar,
        rule_version=rule_version,
        limits=limits,
        tick_sizes=tick_sizes,
        costs=costs,
        starting_equity=starting_equity,
        band=band,
        risk_engine=risk_engine,
        fill_model=fill_model,
    )
    if not results:
        raise ValueError(f"no trading day from {start} to {end} on the Trading Calendar")
    return results


def paper(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    clock: WallClock,
    calendar: TradingCalendar,
    rule_version: str,
    limits: Limits,
    tick_sizes: TickSizes,
    costs: CostModel,
    starting_equity: Decimal = STARTING_EQUITY,
    band: Decimal = EXIT_BAND,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> RunResult:
    """The paper dry-run's day on the wall clock: the session fills the Orders the day before
    placed against the day's end-of-day bar, through the same fill model as a backtest (live
    quotes replace that bar in Phase 3), then one Run over what ``collect-daily`` loaded.
    Every row is tagged paper and marked a dry run, so the Paper Gate never counts it. The
    account is the dry run's own, starting at ``starting_equity``, and its days run in order
    as a backtest's do. ``costs`` should price each Fill on its own day's rates."""
    (result,) = _step(
        conn,
        strategy,
        clock,
        mode="paper",
        backtest_id=None,
        dry_run=True,
        data_source=CollectedMarketData,
        calendar=calendar,
        rule_version=rule_version,
        limits=limits,
        tick_sizes=tick_sizes,
        costs=costs,
        starting_equity=starting_equity,
        band=band,
        risk_engine=risk_engine,
        fill_model=fill_model,
    )
    return result


class Clock(Protocol):
    """A mode's time: the days its Runs are as of, in order."""

    def days(self) -> Iterator[date]: ...


def _step(
    conn: psycopg.Connection,
    strategy: Strategy,
    clock: Clock,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
    data_source: MarketSource,
    **unchanged: Any,
) -> list[RunResult]:
    """The engine's loop: one Run as of each day the clock steps to. The mode, its clock and
    its data source are all that differ between modes; everything else passes through
    ``unchanged``."""
    return [
        run(
            conn,
            strategy,
            mode=mode,
            as_of=day,
            backtest_id=backtest_id,
            dry_run=dry_run,
            data_source=data_source,
            **unchanged,
        )
        for day in clock.days()
    ]


def _next_session(calendar: TradingCalendar, as_of: date) -> date:
    """The trading day after ``as_of``, the session a Run's Orders are for."""
    day = as_of + timedelta(days=1)
    while day <= calendar.last:
        if day in set(calendar.trading_days(day, day)):
            return day
        day += timedelta(days=1)
    raise ValueError(f"the Trading Calendar has no trading day after {as_of}; extend it")


def _previous_session(calendar: TradingCalendar, as_of: date) -> date:
    """The trading day before ``as_of``, the account's last day when days run in order."""
    days = list(calendar.trading_days(max(calendar.first, as_of - timedelta(days=31)), as_of))
    earlier = [d for d in days if d < as_of]
    if not earlier:
        raise ValueError(f"the Trading Calendar has no trading day before {as_of}")
    return earlier[-1]


def _paisa(value: Fraction) -> Decimal:
    """``value`` to the paisa, half up."""
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_down(value: Fraction, tick: Decimal) -> Decimal:
    """``value`` rounded down to the tick, exactly."""
    return math.floor(value / Fraction(tick)) * tick


@dataclass(frozen=True)
class _Order:
    id: uuid.UUID
    run_id: uuid.UUID
    rank: int | None
    position_id: uuid.UUID
    instrument: str
    symbol: str
    purpose: str
    limit_price: Decimal
    shares: int
    session: date

    @property
    def side(self) -> str:
        return "buy" if self.purpose == "entry" else "sell"


class _Account:
    """One account's Orders, Fills and Positions on one day (``data.as_of``), each change
    written to the Ledger as an event in the order it happened.

    The session comes first: the Orders open for the day meet its bar through the
    fill model (``tradeline.paper_broker``), entries and exits at the open, then
    each Position's resting target through the day, including the target placed
    with a Fill that morning. After the Run, ``place_exits`` gives each Position
    still open its one sell Order for the next session: the target re-placed, or,
    when the completed bar triggered a stop or a time stop or an exit failed to
    fill, the target cancelled and the exit placed. ``place_entries`` places a buy
    for each Pass.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        data: StoredMarketData,
        calendar: TradingCalendar,
        *,
        mode: EngineMode,
        backtest_id: uuid.UUID | None,
        dry_run: bool,
        costs: CostModel,
        tick_sizes: TickSizes,
        fill_model: FillModel,
    ) -> None:
        self._conn = conn
        self._dry_run = dry_run
        self._fill_model = fill_model
        self._data = data
        self._calendar = calendar
        self._mode = mode
        self._backtest_id = backtest_id
        self._costs = costs
        self._tick_sizes = tick_sizes
        self._day = data.as_of
        self._seq = 0
        self._positions: dict[uuid.UUID, queries.LedgerPosition] = {}
        # Each open Position's open target Order.
        self._targets: dict[uuid.UUID, _Order] = {}
        # Exits that met the session and did not fill, to be sent again.
        self._exits_due: dict[uuid.UUID, str] = {}
        self._bars: dict[str, Bar | None] = {}

    def open_positions(self) -> list[queries.LedgerPosition]:
        return sorted(self._positions.values(), key=lambda p: (p.opened_on, p.symbol))

    def session(self) -> int:
        """Fill the Orders open for the day against its bar; the number of Fills."""
        account: queries.Account = {
            "mode": self._mode,
            "backtest_id": self._backtest_id,
            "dry_run": self._dry_run,
        }
        open_orders = queries.orders(self._conn, **account, open_only=True)
        stale = sorted({o.session for o in open_orders if o.session != self._day})
        if stale:
            raise ValueError(
                f"the account has Orders open for {stale[0]}, not {self._day};"
                " run its days in order"
            )
        self._positions = {
            p.id: p for p in queries.positions(self._conn, **account, open_only=True)
        }
        ordered = [
            _Order(
                found.id,
                found.run_id,
                found.rank,
                found.position_id,
                found.instrument,
                found.symbol,
                found.purpose,
                found.limit_price,
                found.shares,
                found.session,
            )
            for found in sorted(open_orders, key=lambda o: (o.purpose != "entry", o.placed))
        ]
        fills = 0
        for order in self._restate(ordered):
            if order.purpose == "entry":
                fills += self._meet_entry(order)
            elif order.purpose == "target":
                self._targets[order.position_id] = order
            else:
                fills += self._meet_exit(order)
        for target in list(self._targets.values()):
            fills += self._meet_target(target)
        return fills

    def _restate(self, orders: list[_Order]) -> list[_Order]:
        """Restate each held Position whose Instrument has a split or bonus ex-dated since the
        account's last trading day, and its Orders open for the day; the Orders that remain
        open, in their order."""
        instruments = {p.instrument for p in self._positions.values()}
        instruments |= {o.instrument for o in orders}
        factors = {i: f for i in sorted(instruments) if (f := self._factor(i)) is not None}
        if not factors:
            return orders
        for position in self.open_positions():
            if position.instrument in factors:
                self._restate_position(position, *factors[position.instrument])
        remaining = []
        for order in orders:
            if order.instrument not in factors:
                remaining.append(order)
                continue
            # NSE purges an Instrument's open Orders on its Ex-Date.
            self._cancel(order, "corporate_action")
            if order.purpose == "entry":
                continue
            position = self._positions[order.position_id]
            if order.purpose == "target":
                limit = position.target
            else:
                factor, _ = factors[order.instrument]
                limit = _round_down(Fraction(order.limit_price) * factor, self._tick(position))
            again = replace(order, id=uuid.uuid4(), limit_price=limit, shares=position.shares)
            self._place(again)
            remaining.append(again)
        return remaining

    def _factor(self, instrument: str) -> tuple[Fraction, Decimal] | None:
        """The Adjustment Factor of the splits and bonuses ex-dated in the Instrument since the
        account's last trading day, and its last close then in the day's prices; None if
        there are none."""
        previous = _previous_session(self._calendar, self._day)
        actions = queries.corporate_actions(
            self._conn,
            instrument,
            start=previous + timedelta(days=1),
            end=self._day,
            as_of=self._day,
        )
        if not any(a.kind in ADJUSTED_KINDS for a in actions):
            return None
        # One bar read as of the day and as of the last trading day: the ratio of its factors
        # is the actions between, de-duplicated as the Adjusted Series applies them.
        now = [
            b
            for b in self._data.bars(instrument, start=previous - REFERENCE_LOOKBACK, end=previous)
            if b.close is not None
        ]
        if not now:
            raise ValueError(f"{instrument} has no close in the month to {previous}")
        last = now[-1]
        (then,) = queries.adjusted_daily_candles(
            self._conn, instrument, start=last.trade_date, end=last.trade_date, as_of=previous
        )
        factor = last.factor / then.factor
        if factor == 1:
            return None
        assert last.close is not None
        return factor, last.close

    def _restate_position(
        self, position: queries.LedgerPosition, factor: Fraction, close: Decimal
    ) -> None:
        exact = Fraction(position.shares) / factor
        shares = math.floor(exact)
        if shares == 0:
            raise ValueError(
                f"{position.symbol}'s {position.shares} shares come to none after the"
                f" Adjustment Factor {factor} on {self._day}; close the Position by hand"
            )
        tick = self._tick(position)
        restated = replace(
            position,
            shares=shares,
            entry=_paisa(Fraction(position.entry) * factor),
            stop=_round_down(Fraction(position.stop) * factor, tick),
            target=_round_down(Fraction(position.target) * factor, tick),
            as_of=self._day,
        )
        self._positions[position.id] = restated
        self._write_position(
            restated,
            event="restated",
            factor=factor,
            cash_in_lieu=_paisa((exact - shares) * Fraction(close)),
        )

    def _tick(self, position: queries.LedgerPosition) -> Decimal:
        return self._tick_sizes.tick(
            self._day, reference_close=self._data.reference_close(position.instrument)
        )

    def close(self, instrument: str) -> Decimal:
        """The Instrument's latest close on or before the day."""
        bar = self._bar(instrument)
        if bar is not None:
            return bar.close
        closes = [b.close for b in self._data.last_bars(instrument, 10) if b.close is not None]
        if not closes:
            raise ValueError(f"{instrument} has no close in the ten trading days to {self._day}")
        return closes[-1]

    def place_exits(
        self, *, run_id: uuid.UUID, session: date, band: Decimal, tick_sizes: TickSizes
    ) -> int:
        """One sell Order for the next session for each open Position; the number placed."""
        for position in self.open_positions():
            reason = self._exits_due.get(position.id)
            bar = self._bar(position.instrument)
            if reason is None and bar is not None:
                held = len(
                    list(
                        self._calendar.trading_days(
                            position.opened_on + timedelta(days=1), self._day
                        )
                    )
                )
                reason = self._fill_model.trigger(
                    bar,
                    stop=position.stop,
                    days_held=held,
                    time_stop_days=position.time_stop_days,
                )
            target = self._targets.pop(position.id, None)
            if target is not None:
                # The Position's one sell Order goes before another is placed.
                self._cancel(target, reason or "replaced")
            if reason is None:
                limit = position.target
            else:
                tick = tick_sizes.tick(
                    self._day,
                    reference_close=self._data.reference_close(position.instrument),
                )
                limit = self._fill_model.exit_limit(
                    self.close(position.instrument), band=band, tick=tick
                )
            self._place(
                _Order(
                    uuid.uuid4(),
                    run_id,
                    None,
                    position.id,
                    position.instrument,
                    position.symbol,
                    reason or "target",
                    limit,
                    position.shares,
                    session,
                )
            )
        return len(self._positions)

    def place_entries(self, passes: Sequence[Pass], *, run_id: uuid.UUID, session: date) -> int:
        """A buy for the next session at each Pass's entry limit; the number placed."""
        for granted in passes:
            verify_pass(granted, granted.plan, run_id=run_id)
            plan = granted.plan
            self._place(
                _Order(
                    uuid.uuid4(),
                    run_id,
                    plan.rank,
                    uuid.uuid4(),
                    plan.instrument,
                    plan.symbol,
                    "entry",
                    plan.entry,
                    plan.shares,
                    session,
                )
            )
        return len(passes)

    def _bar(self, instrument: str) -> Bar | None:
        if instrument not in self._bars:
            found = self._data.bars(instrument, start=self._day, end=self._day)
            self._bars[instrument] = None
            if found:
                (day,) = found
                o, h, low, c = day.open, day.high, day.low, day.close
                # A row without prices traded nothing: no bar.
                if o is not None and h is not None and low is not None and c is not None:
                    self._bars[instrument] = Bar(o, h, low, c)
        return self._bars[instrument]

    def _meet_entry(self, order: _Order) -> int:
        outcome = self._fill_model.entry(order.limit_price, self._bar(order.instrument))
        if isinstance(outcome, Unfilled):
            self._cancel(order, outcome.reason)
            return 0
        assert order.rank is not None
        (granted,) = (p for p in queries.passes(self._conn, order.run_id) if p.rank == order.rank)
        self._fill(order, outcome.price)
        position = queries.LedgerPosition(
            id=order.position_id,
            run_id=order.run_id,
            rank=order.rank,
            instrument=order.instrument,
            symbol=order.symbol,
            opened_on=self._day,
            shares=order.shares,
            entry=outcome.price,
            stop=granted.stop,
            target=granted.target,
            time_stop_days=granted.time_stop_days,
            as_of=self._day,
            exit_reason=None,
            exit_price=None,
        )
        self._positions[position.id] = position
        self._write_position(position)
        # The target rests with the broker from the Fill.
        target = replace(
            order, id=uuid.uuid4(), rank=None, purpose="target", limit_price=granted.target
        )
        self._place(target)
        self._targets[position.id] = target
        return 1

    def _meet_exit(self, order: _Order) -> int:
        outcome = self._fill_model.exit(order.limit_price, self._bar(order.instrument))
        if isinstance(outcome, Unfilled):
            self._cancel(order, outcome.reason)
            self._exits_due[order.position_id] = order.purpose
            return 0
        self._fill(order, outcome.price)
        self._close_position(order.position_id, price=outcome.price, reason=order.purpose)
        return 1

    def _meet_target(self, order: _Order) -> int:
        position = self._positions[order.position_id]
        outcome = self._fill_model.target(
            order.limit_price, self._bar(order.instrument), stop=position.stop
        )
        if isinstance(outcome, Unfilled):
            return 0
        del self._targets[order.position_id]
        self._fill(order, outcome.price)
        self._close_position(order.position_id, price=outcome.price, reason="target")
        return 1

    def _close_position(self, position_id: uuid.UUID, *, price: Decimal, reason: str) -> None:
        closed = replace(
            self._positions.pop(position_id),
            as_of=self._day,
            exit_reason=reason,
            exit_price=price,
        )
        self._write_position(closed)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _write_order(self, order: _Order, *, event: str, reason: str | None) -> None:
        self._conn.execute(
            "INSERT INTO orders (mode, backtest_id, dry_run, id, event, day, seq, reason, run_id,"
            " rank, position_id, instrument, symbol, side, purpose, limit_price, shares, session)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self._mode,
                self._backtest_id,
                self._dry_run,
                order.id,
                event,
                self._day,
                self._next_seq(),
                reason,
                order.run_id,
                order.rank,
                order.position_id,
                order.instrument,
                order.symbol,
                order.side,
                order.purpose,
                order.limit_price,
                order.shares,
                order.session,
            ),
        )

    def _place(self, order: _Order) -> None:
        self._write_order(order, event="placed", reason=None)

    def _cancel(self, order: _Order, reason: str) -> None:
        self._write_order(order, event="cancelled", reason=reason)

    def _fill(self, order: _Order, price: Decimal) -> None:
        """The Fill, with its charges and Slippage from the cost model; the Order's share of
        traded value is of the 20 trading days to the Fill's."""
        charges = self._costs.charges(
            order.side,
            day=self._day,
            price=price,
            shares=order.shares,
            average_traded_value=self._data.average_traded_value(
                order.instrument, TRADED_VALUE_DAYS
            ),
        )
        self._conn.execute(
            "INSERT INTO fills (mode, backtest_id, dry_run, order_id, day, seq, position_id,"
            " instrument, symbol, side, price, shares, "
            + ", ".join(charges.items())
            + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
            + ", %s" * len(charges.items())
            + ")",
            (
                self._mode,
                self._backtest_id,
                self._dry_run,
                order.id,
                self._day,
                self._next_seq(),
                order.position_id,
                order.instrument,
                order.symbol,
                order.side,
                price,
                order.shares,
                *charges.items().values(),
            ),
        )

    def _write_position(
        self,
        position: queries.LedgerPosition,
        *,
        event: str | None = None,
        factor: Fraction | None = None,
        cash_in_lieu: Decimal | None = None,
    ) -> None:
        """The Position's state after an event: ``opened`` or ``closed`` by its exit unless
        ``event`` says otherwise."""
        self._conn.execute(
            "INSERT INTO positions (mode, backtest_id, dry_run, position_id, event, day, seq,"
            " run_id, rank, instrument, symbol, opened_on, shares, entry, stop, target,"
            " time_stop_days, exit_price, exit_reason, factor, cash_in_lieu)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s)",
            (
                self._mode,
                self._backtest_id,
                self._dry_run,
                position.id,
                event or ("opened" if position.exit_reason is None else "closed"),
                self._day,
                self._next_seq(),
                position.run_id,
                position.rank,
                position.instrument,
                position.symbol,
                position.opened_on,
                position.shares,
                position.entry,
                position.stop,
                position.target,
                position.time_stop_days,
                position.exit_price,
                position.exit_reason,
                None if factor is None else f"{factor.numerator}/{factor.denominator}",
                cash_in_lieu,
            ),
        )


def _holdings(
    held: Sequence[queries.LedgerPosition], account: _Account, sectors: dict[str, str]
) -> tuple[Holding, ...]:
    """Each open Position as the Limits see it: its sector as of the Run (from ``sectors``)
    and its value at the day's close."""
    return tuple(
        Holding(p.instrument, sectors.get(p.symbol), p.shares * account.close(p.instrument))
        for p in held
    )


def _insert_held_verdicts(
    conn: psycopg.Connection,
    plans: Sequence[Plan],
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
    run_id: uuid.UUID,
) -> None:
    """SKIP, reason ``held``, for each Candidate whose Instrument the account already holds."""
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO verdicts (mode, backtest_id, dry_run, run_id, rank, verdict, reason)"
            " VALUES (%s, %s, %s, %s, %s, 'SKIP', 'held')",
            [(mode, backtest_id, dry_run, run_id, p.rank) for p in plans],
        )


def _proposals(
    plans: Sequence[Plan],
    data: StoredMarketData,
    tick_sizes: TickSizes,
    sectors: dict[str, str],
    *,
    as_of: date,
) -> list[Proposal]:
    """Each Plan with what the Limits read about its Instrument as of the Run."""
    return [
        Proposal(
            plan,
            tick=tick_sizes.tick(as_of, reference_close=data.reference_close(plan.instrument)),
            sector=sectors.get(plan.symbol),
            average_traded_value=data.average_traded_value(plan.instrument, TRADED_VALUE_DAYS),
        )
        for plan in plans
    ]


@functools.cache
def _code_revision(module: str) -> str:
    """The code revision, hashed once a process: a backtest records it every Run, and the
    code a process runs cannot change under it."""
    return code_revision(module)


def _strategy_row(
    conn: psycopg.Connection,
    strategy: Strategy,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
) -> uuid.UUID:
    revision = _code_revision(type(strategy).__module__)
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
    dry_run: bool,
    strategy_id: uuid.UUID,
    as_of: date,
    rule_version: str,
    universe_size: int,
    cash: Decimal,
    equity: Decimal,
) -> None:
    conn.execute(
        "INSERT INTO runs (id, mode, backtest_id, dry_run, strategy_id, as_of, universe_month,"
        " universe_rule_version, universe_size, parameters, cash, equity)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            mode,
            backtest_id,
            dry_run,
            strategy_id,
            as_of,
            month_of(as_of),
            rule_version,
            universe_size,
            json.dumps(dict(strategy.parameters()), sort_keys=True),
            cash,
            equity,
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
    dry_run: bool,
    run_id: uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO candidates (mode, backtest_id, dry_run, run_id, rank, instrument,"
            " symbol, score, facts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mode,
                    backtest_id,
                    dry_run,
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
    dry_run: bool,
    run_id: uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO plans (mode, backtest_id, dry_run, run_id, rank, instrument, symbol,"
            " entry, stop, target, time_stop_days, facts)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mode,
                    backtest_id,
                    dry_run,
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
    dry_run: bool,
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
                    "INSERT INTO passes (mode, backtest_id, dry_run, run_id, rank, plan_hash,"
                    " entry, stop, target, time_stop_days, shares, sector, facts)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        mode,
                        backtest_id,
                        dry_run,
                        decision.run_id,
                        plan.rank,
                        decision.plan_hash,
                        *priced,
                    ),
                )
                verdict, reason = "BUY", "pass"
            else:
                cursor.execute(
                    "INSERT INTO blocks (mode, backtest_id, dry_run, run_id, rank, limit_name,"
                    " entry, stop, target, time_stop_days, shares, sector, facts)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        mode,
                        backtest_id,
                        dry_run,
                        decision.run_id,
                        plan.rank,
                        decision.limit,
                        *priced,
                    ),
                )
                verdict, reason = "SKIP", decision.limit
            cursor.execute(
                "INSERT INTO verdicts (mode, backtest_id, dry_run, run_id, rank, verdict,"
                " reason) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (mode, backtest_id, dry_run, decision.run_id, plan.rank, verdict, reason),
            )
