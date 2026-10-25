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

In backtest mode the clock is the Backtest's: ``BacktestClock`` steps one
trading day at a time over the Trading Calendar, and each step is one Run as of
that day. The data source, ``StoredMarketData``, is bound to the Run's As-Of Date and
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

When the Protocol credits dividends (ADR 0020), before the session each Position
held at the account's last close is credited, in cash on the Ex-Date, every
dividend its Instrument has ex-dated since then: its shares times the amount
per share NSE's subject states (``tradeline.dividends``). A Position bought at
the Ex-Date's open held nothing at the close before and is credited nothing; one
sold that day is credited. An unparsed subject credits nothing and is written
with its reason. The credit is part of the account's cash, so of Equity.

A backtest runs under a frozen Protocol (``tradeline.protocol``, ADR 0007)
and nothing else: its Universe Rule version, Limits, costs, band and starting
Equity are the Protocol's, and each Strategy version it records names the
Protocol. It is refused when it names none, when the code is not at the commit
the Protocol was frozen at, when its Strategy or its parameters are not the
Protocol's, when its range runs outside the Protocol's, and when a Universe
month or an Adjusted Series it would read is not what the Protocol froze. A
single day, ``run``, and the paper dry-run take their terms from their caller.
"""

from __future__ import annotations

import functools
import logging
import math
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Protocol, TypeVar

import psycopg

from tradeline import ledger, queries, revision
from tradeline.adjust import ADJUSTED_KINDS, AdjustedBar, CorporateAction
from tradeline.costs import CostModel, paisa
from tradeline.dates import IST, month_of
from tradeline.dividends import dividends as dividends_of
from tradeline.freeze import verify_snapshot
from tradeline.paper_broker import (
    FILL_MODEL,
    AfterCloseExit,
    Bar,
    ExitReason,
    FillModel,
    OrderPurpose,
    Unfilled,
)
from tradeline.queries import EngineMode
from tradeline.revision import code_revision
from tradeline.risk import (
    RISK_ENGINE,
    Holding,
    Limits,
    Pass,
    Portfolio,
    PortfolioPlan,
    Proposal,
    RiskEngine,
    verify_pass,
)
from tradeline.strategy import Candidate, Member, Plan, Strategy
from tradeline.strategy.portfolio import AnyStrategy, PortfolioStrategy
from tradeline.strategy.pricing import round_down
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)

T = TypeVar("T")

# The trading days the Participation Cap averages traded value over.
TRADED_VALUE_DAYS = 20
# How far before a backtest's first day the SeriesCache loads each Instrument's rows.
SERIES_LOOKBACK = timedelta(days=730)
# How far before the month the close that fixes the tick is looked for.
REFERENCE_LOOKBACK = timedelta(days=31)


class BacktestClock:
    """A Backtest's time: the Trading Calendar's open days from ``start`` to ``end``."""

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
    layer instead.

    It also remembers what each day's reads found (``remember``), each under the day it
    was read as of, so backtests over the same days share one cache: Walk-Forward's grid
    cells in a Window, and the Window's Out-of-Sample run, load the Window's bars once and
    read each day's market once."""

    def __init__(self, conn: psycopg.Connection, *, start: date, end: date) -> None:
        self._conn = conn
        self._start, self._end = start - SERIES_LOOKBACK, end
        self._spans: dict[str, queries.AdjustedSeriesSpan] = {}
        # Symbols read as of one day: what each reads as then.
        self._resolved: dict[str, str] = {}
        self._resolved_on: date | None = None
        self._remembered: dict[tuple[Any, ...], Any] = {}

    def remember(self, key: tuple[Any, ...], read: Callable[[], T]) -> T:
        """What ``read`` returns, read once for ``key``; the key must name the As-Of Date
        and everything else the read depends on."""
        if key not in self._remembered:
            self._remembered[key] = read()
        found: T = self._remembered[key]
        return found

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

    def screen(self, strategy: Strategy, members: Sequence[Member]) -> list[Candidate]:
        """The Strategy's ranked Candidates among ``members`` as of the day. Through a
        backtest's cache, Strategies of one class that read the same screen parameters
        (``Strategy.screen_parameters``) screen a day once between them."""
        if self._cache is None:
            return strategy.screen(members, self, self._as_of)
        kind = type(strategy)
        key = (
            "screen",
            self._as_of,
            f"{kind.__module__}.{kind.__qualname__}",
            tuple(sorted(strategy.screen_parameters().items())),
            tuple(members),
        )
        return self._cache.remember(key, lambda: strategy.screen(members, self, self._as_of))

    def average_traded_value(self, instrument: str, days: int) -> Decimal:
        """The rupees traded a day over the last ``days`` trading days up to ``as_of``, a
        day without trading counting as none."""
        return self._remember(
            ("traded_value", instrument, days), lambda: self._average_traded_value(instrument, days)
        )

    def _remember(self, key: tuple[Any, ...], read: Callable[[], T]) -> T:
        """``read`` through the backtest's cache, if any, as of the day."""
        if self._cache is None:
            return read()
        return self._cache.remember((*key, self._as_of), read)

    def _average_traded_value(self, instrument: str, days: int) -> Decimal:
        start = self._last_days(days)[0]
        rows = queries.end_of_day(
            self._conn, instrument, start=start, end=self._as_of, as_of=self._as_of
        )
        return sum((r.traded_value for r in rows), Decimal(0)) / days

    def reference_close(self, instrument: str) -> Decimal:
        """The close NSE fixes a security's tick by for the month (NSE/CMTR/62174): the latest
        close on or before the last day of the month before; for a security first traded
        this month, its first close."""
        return self._remember(
            ("reference_close", instrument), lambda: self._reference_close(instrument)
        )

    def _reference_close(self, instrument: str) -> Decimal:
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
        asked = sorted(set(symbols))
        if self._cache is None:
            found = queries.sectors_for_run(self._conn, asked, as_of=self._as_of)
            return {s.symbol: s.sector for s in found}
        # A Symbol's sector does not depend on the others asked with it, so the day's sectors
        # are kept by Symbol (None: Unclassified) and only those not yet read are asked for.
        known: dict[str, str | None] = self._cache.remember(("sectors", self._as_of), dict)
        unknown = [s for s in asked if s not in known]
        if unknown:
            found = queries.sectors_for_run(self._conn, unknown, as_of=self._as_of)
            sectors = {s.symbol: s.sector for s in found}
            known.update({s: sectors.get(s) for s in unknown})
        return {s: sector for s in asked if (sector := known[s]) is not None}

    def corporate_actions(self, instrument: str, *, start: date) -> list[CorporateAction]:
        """The Instrument's actions ex-dated from ``start`` to the As-Of Date."""
        return self._remember(
            ("corporate_actions", instrument, start),
            lambda: queries.corporate_actions(
                self._conn, instrument, start=start, end=self._as_of, as_of=self._as_of
            ),
        )

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
    strategy: AnyStrategy,
    *,
    mode: EngineMode,
    as_of: date,
    calendar: TradingCalendar,
    rule_version: str,
    limits: Limits,
    tick_sizes: TickSizes,
    costs: CostModel,
    starting_equity: Decimal,
    band: Decimal,
    backtest_id: uuid.UUID | None = None,
    dry_run: bool = False,
    data_source: MarketSource = StoredMarketData,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
    protocol: str | None = None,
    impact: Decimal | None = None,
    entries: bool = True,
    boundary: bool = False,
    commit: bool = True,
    initial_buy: bool = False,
    dividends: bool = False,
    halted: bool = False,
) -> RunResult:
    """The day ``as_of`` in one account: its held Positions credited any dividend ex-dated
    since its last day when ``dividends`` is on (as its Protocol states), and restated for
    any split or bonus, its session, then one Run over the month's Universe under
    ``rule_version``, its Plans checked against ``limits`` at the account's Equity (from
    ``starting_equity``, after ``costs``) with its open Positions held, then the Orders for
    the next session.

    The market is read from ``data_source`` as of the day; the Strategy, ``risk_engine``,
    ``costs`` and ``fill_model`` are used as given, whatever the mode. A backtest id is
    required in backtest mode and refused in any other; ``dry_run`` marks a paper dry-run's
    rows (an account of its own), and only a paper Run can be one. ``protocol`` is the hash
    of the Protocol the Strategy version runs under, if any; ``impact`` the Slippage impact c
    ``costs`` was given in place of the Protocol's, recorded on the Run.

    Without ``entries`` the Run screens nothing, so it plans no new Position: its held
    Positions keep their exits. With ``boundary`` every Position open after the session
    takes a Boundary Exit at the day's close, after costs, before the Run's Equity is
    taken, and no Order is placed for the next session: the account ends with the day.
    ``commit`` false leaves the day uncommitted, for a caller that holds the transaction
    (Walk-Forward's grid cells, which keep only a summary).

    A portfolio Strategy (``PortfolioStrategy``) screens and plans nothing. On the last
    trading day of one of its ``rebalance_months`` the Run is a Rebalance: its entrants
    are the Run's Candidates, and each leaver gets a sell at the close less the band for
    the next session. On the trading day after, the Run sizes the entrants from the cash
    then held (``_split_cash``) and puts them to the Risk Engine, and each Pass is a buy
    for the next session. ``initial_buy`` makes the Run its account's Initial Buy, on
    the trading day before the account's first: a Rebalance's entrants with nothing
    held, sized and put to the Risk Engine at once, bought at the next open. On every
    other day its holdings rest, with no Order but a sale the open did not fill.

    ``halted`` is a Run under Halt (ADR 0009): it places no new entry and keeps managing
    exits. A Run of a Strategy with Plans screens nothing; a portfolio Rebalance still
    decides and sells its leavers, and its buys (or an Initial Buy's) place nothing (ADR
    0020). A backtest never
    Halts (``docs/risk-controls.md``), so only paper passes it.
    """
    if boundary and entries:
        raise ValueError("a Boundary Exit's day places no new entries; pass entries=False")
    if (mode == "backtest") != (backtest_id is not None):
        raise ValueError("a backtest id is required in backtest mode and only there")
    if dry_run and mode != "paper":
        raise ValueError("only a paper Run can be a dry run")
    if as_of not in set(calendar.trading_days(as_of, as_of)):
        raise ValueError(f"{as_of} is not a trading day on the Trading Calendar")
    if initial_buy and not isinstance(strategy, PortfolioStrategy):
        raise ValueError(f"{strategy.name} is not a portfolio Strategy; it has no Initial Buy")
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
        dividends=dividends,
    )
    tags: queries.Account = {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run}
    next_session = _next_session(calendar, as_of)
    step: str | None = None
    candidates: list[Candidate] = []
    plans: list[Plan | PortfolioPlan] = []
    if isinstance(strategy, PortfolioStrategy):
        step = _portfolio_step(
            conn,
            strategy,
            calendar,
            tags,
            as_of=as_of,
            next_session=next_session,
            initial_buy=initial_buy,
            entries=entries,
        )
    else:
        candidates = data.screen(strategy, members) if entries and not halted else []
        plans = [strategy.plan(c) for c in candidates]
    leavers: tuple[str, ...] = ()
    run_id = uuid.uuid4()
    # Pipelined: the day's writes go without waiting each for its reply (#82: a backtest is
    # bound by round trips to Postgres, not by its own work).
    with conn.pipeline(), conn.transaction():
        fills = account.session()
        if boundary:
            fills += account.boundary_exits()
        held = account.open_positions()
        held_instruments = {p.instrument for p in held}
        if isinstance(strategy, PortfolioStrategy):
            candidates, leavers = _portfolio_decision(
                conn, strategy, step, held, members, data, calendar, tags, as_of=as_of
            )
        sectors = data.sectors(
            [m.symbol for m in members] + [p.symbol for p in held] + [c.symbol for c in candidates]
        )
        holdings = _holdings(held, account, sectors)
        cash = starting_equity + queries.account_cash_flows(conn, **tags)
        equity = cash + sum((h.value for h in holdings), Decimal(0))
        if step in ("initial_buy", "rebalance_buys") and not halted:
            plans = list(
                _split_cash(
                    [c for c in candidates if c.instrument not in held_instruments],
                    cash=cash,
                    account=account,
                    band=band,
                    session=next_session,
                )
            )
        # One Position per Instrument: a Candidate already held is skipped, not assessed.
        to_assess = [p for p in plans if p.instrument not in held_instruments]
        proposals = _proposals(to_assess, data, tick_sizes, sectors, as_of=as_of)
        portfolio = Portfolio(equity, holdings)
        decisions = risk_engine.assess(proposals, portfolio=portfolio, limits=limits, run_id=run_id)
        strategy_id = ledger.strategy_row(
            conn,
            strategy,
            mode=mode,
            backtest_id=backtest_id,
            protocol=protocol,
            code_revision=_code_revision(type(strategy).__module__),
        )
        ledger.insert_run(
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
            impact=impact,
            portfolio_step=step,
        )
        ledger.insert_candidates(conn, candidates, **tags, run_id=run_id)
        ledger.insert_plans(conn, plans, **tags, run_id=run_id)
        ledger.insert_decisions(conn, decisions, proposals, equity=equity, **tags)
        ledger.insert_held_verdicts(
            conn, [p for p in plans if p.instrument in held_instruments], **tags, run_id=run_id
        )
        placed = 0
        if not boundary:
            placed = account.place_exits(
                run_id=run_id, session=next_session, band=band, tick_sizes=tick_sizes
            )
            placed += account.place_sales(
                leavers, run_id=run_id, session=next_session, band=band, tick_sizes=tick_sizes
            )
            placed += account.place_entries(
                [d for d in decisions if isinstance(d, Pass)], run_id=run_id, session=next_session
            )
        still_open = len(account.open_positions())
    if commit:
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
    strategy: AnyStrategy,
    *,
    protocol: str | None,
    start: date,
    end: date,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
    costs: CostModel | None = None,
    impact: Decimal | None = None,
) -> list[RunResult]:
    """A backtest under the frozen Protocol ``protocol`` (its hash, or 12 or more characters
    of it) from ``start`` to ``end``: one day per step of the Backtest's clock, each filling
    the day before's Orders and then Running, all under one backtest id, one account
    starting at the Protocol's starting Equity, every trade priced on its cost schedule's
    date, and every Run under its Universe Rule version, Limits and band.

    ``costs`` is the cost model to price with when the caller holds one (the same object a
    paper account uses); it is refused unless it prices as the Protocol states.

    ``impact`` replaces the Protocol's Slippage impact c, for the report's c sensitivity
    (c at zero and at twice its value, as information): every Run of such a backtest
    records the c it was priced with, so it is never read as the Protocol's result."""
    frozen = _protocol_for(conn, strategy, protocol, start=start, end=end)
    return backtest_span(
        conn,
        strategy,
        frozen,
        start=start,
        end=end,
        calendar=calendar,
        tick_sizes=tick_sizes,
        backtest_id=uuid.uuid4(),
        cache=SeriesCache(conn, start=start, end=end),
        risk_engine=risk_engine,
        fill_model=fill_model,
        costs=costs,
        impact=impact,
    )


def backtest_span(
    conn: psycopg.Connection,
    strategy: AnyStrategy,
    frozen: queries.FrozenProtocol,
    *,
    start: date,
    end: date,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    backtest_id: uuid.UUID,
    cache: SeriesCache,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
    costs: CostModel | None = None,
    impact: Decimal | None = None,
    entries: bool = True,
    boundary: bool = False,
    commit: bool = True,
    fresh_period: bool = False,
) -> list[RunResult]:
    """``backtest``'s days from ``start`` to ``end`` in the account ``backtest_id``, which
    may already hold the days before ``start``, reading the market through ``cache``.

    Every refusal of ``backtest`` but one applies: the data snapshot is not verified here,
    since a caller running many spans over one Protocol verifies it once
    (``freeze.verify_snapshot``) for all of them. ``entries``, ``boundary`` (a Boundary
    Exit on ``end``) and ``commit`` are ``run``'s.

    ``fresh_period`` is for a Not Independent version's Fresh Period alone
    (``holdout.run_fresh_period``): its days lie after the Protocol's Holdout, past what
    the Protocol froze, so the range must start after the Holdout's last day instead of
    lying inside the Protocol's.

    A portfolio Strategy's account that has no Run yet starts with its Initial Buy, a Run
    as of the trading day before ``start`` (``run``'s ``initial_buy``), unless the span
    places no entries."""
    _protocol_for(
        conn,
        strategy,
        frozen.hash,
        start=start,
        end=end,
        verify=False,
        fresh_period=fresh_period,
    )
    terms = frozen.protocol
    if costs is None:
        costs = terms.cost_model()
    elif (costs.rates_as_of, costs.slippage) != (terms.costs_as_of, terms.slippage) or (
        costs.schedule.rates(terms.costs_as_of) != terms.rates
    ):
        raise ValueError(
            f"the cost model does not price as Protocol {frozen.hash[:12]} states: its"
            f" schedule as of {terms.costs_as_of} with Slippage {terms.slippage}"
        )
    if impact is not None:
        costs = replace(costs, slippage=replace(costs.slippage, impact=impact))
    days = list(BacktestClock(calendar, start=start, end=end).days())
    if not days:
        raise ValueError(f"no trading day from {start} to {end} on the Trading Calendar")
    terms_of_run: dict[str, Any] = {
        "mode": "backtest",
        "backtest_id": backtest_id,
        "dry_run": False,
        "data_source": cache.market_data,
        "calendar": calendar,
        "rule_version": terms.rule_version,
        "limits": terms.limits,
        "tick_sizes": tick_sizes,
        "costs": costs,
        "starting_equity": terms.starting_equity,
        "band": terms.band,
        "risk_engine": risk_engine,
        "fill_model": fill_model,
        "protocol": frozen.hash,
        "impact": impact,
        "commit": commit,
        "dividends": terms.dividends,
    }
    last = days[-1]
    before = days[:-1] if boundary else days
    results = []
    if (
        isinstance(strategy, PortfolioStrategy)
        and entries
        and not queries.runs(conn, mode="backtest", backtest_id=backtest_id)
    ):
        decided = _previous_session(calendar, days[0])
        results = _step(
            conn,
            strategy,
            BacktestClock(calendar, start=decided, end=decided),
            entries=True,
            initial_buy=True,
            **terms_of_run,
        )
    if before:
        results += _step(
            conn,
            strategy,
            BacktestClock(calendar, start=start, end=before[-1]),
            entries=entries,
            **terms_of_run,
        )
    if boundary:
        results += _step(
            conn,
            strategy,
            BacktestClock(calendar, start=last, end=last),
            entries=False,
            boundary=True,
            **terms_of_run,
        )
    return results


def paper(
    conn: psycopg.Connection,
    strategy: AnyStrategy,
    *,
    clock: WallClock,
    calendar: TradingCalendar,
    rule_version: str,
    limits: Limits,
    tick_sizes: TickSizes,
    costs: CostModel,
    starting_equity: Decimal,
    band: Decimal,
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
    strategy: AnyStrategy,
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


def _protocol_for(
    conn: psycopg.Connection,
    strategy: AnyStrategy,
    name: str | None,
    *,
    start: date,
    end: date,
    verify: bool = True,
    fresh_period: bool = False,
) -> queries.FrozenProtocol:
    """The frozen Protocol a backtest of ``strategy`` from ``start`` to ``end`` may run
    under, or the reason it may not; its data snapshot verified unless ``verify`` is
    false. A ``fresh_period`` range runs after the Protocol's Holdout instead of inside
    the Protocol's range."""
    if not name:
        raise ValueError(
            "a backtest runs under a frozen Protocol and names none;"
            " freeze one with `tradeline protocol freeze` and name it"
        )
    frozen = queries.protocol(conn, name)
    if frozen is None:
        raise ValueError(f"no Protocol {name} is frozen; `tradeline protocol freeze` freezes one")
    terms, short = frozen.protocol, frozen.hash[:12]
    commit = revision.running_commit()
    if terms.git_commit != commit:
        raise ValueError(
            f"Protocol {short} was frozen at commit {terms.git_commit} and this is commit"
            f" {commit}; a run under it must share its commit, so freeze a new Protocol at"
            " this commit and rerun its Windows"
        )
    strategy_revision = _code_revision(type(strategy).__module__)
    if (terms.family, terms.name, terms.code_revision) != (
        strategy.family,
        strategy.name,
        strategy_revision,
    ):
        raise ValueError(
            f"Protocol {short} validates {terms.name} ({terms.family}) at code revision"
            f" {terms.code_revision[:12]}, not {strategy.name} ({strategy.family}) at"
            f" {strategy_revision[:12]}"
        )
    parameters = strategy.parameters()
    for axis, values in terms.grid.items():
        if Decimal(parameters[axis]) not in values:
            raise ValueError(
                f"{axis} = {parameters[axis]} is not on Protocol {short}'s grid"
                f" ({', '.join(str(v) for v in values)})"
            )
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    if fresh_period:
        if start <= terms.holdout.last:
            raise ValueError(
                f"a Fresh Period starts after Protocol {short}'s Holdout, which ends"
                f" {terms.holdout.last}, not on {start}"
            )
    elif start < terms.first or end > terms.last:
        raise ValueError(
            f"{start} to {end} runs outside Protocol {short}'s {terms.first} to {terms.last}"
        )
    if verify:
        verify_snapshot(conn, frozen, start=start, end=end)
    return frozen


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
        dividends: bool = False,
    ) -> None:
        self._conn = conn
        self._dividends = dividends
        self._account: queries.Account = {
            "mode": mode,
            "backtest_id": backtest_id,
            "dry_run": dry_run,
        }
        self._fill_model = fill_model
        self._data = data
        self._calendar = calendar
        self._costs = costs
        self._tick_sizes = tick_sizes
        self._day = data.as_of
        self._seq = 0
        self._positions: dict[uuid.UUID, queries.LedgerPosition] = {}
        # Each open Position's open target Order.
        self._targets: dict[uuid.UUID, ledger.Order] = {}
        # Exits that met the session and did not fill, to be sent again.
        self._exits_due: dict[uuid.UUID, AfterCloseExit] = {}
        # Positions given their one sell Order for the next session by this Run.
        self._selling: set[uuid.UUID] = set()
        self._bars: dict[str, Bar | None] = {}

    def open_positions(self) -> list[queries.LedgerPosition]:
        return sorted(self._positions.values(), key=lambda p: (p.opened_on, p.symbol))

    def session(self) -> int:
        """Fill the Orders open for the day against its bar; the number of Fills."""
        account = self._account
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
            ledger.Order(
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
        if self._dividends:
            self._credit_dividends()
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

    def _credit_dividends(self) -> None:
        """Credit each Position open before the session, so held at the last close, every
        dividend its Instrument has ex-dated since the account's last trading day, on the
        shares held then (before any split or bonus that day restates them). An unparsed
        subject credits nothing and is written with its reason."""
        previous = _previous_session(self._calendar, self._day)
        for position in self.open_positions():
            actions = self._data.corporate_actions(
                position.instrument, start=previous + timedelta(days=1)
            )
            for dividend in dividends_of(position.instrument, actions):
                ledger.insert_dividend_credit(
                    self._conn,
                    self._account,
                    position,
                    dividend,
                    day=self._day,
                    seq=self._next_seq(),
                )

    def _restate(self, orders: list[ledger.Order]) -> list[ledger.Order]:
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
                # Only a Position with exits has a target.
                assert position.target is not None
                limit = position.target
            else:
                factor, _ = factors[order.instrument]
                limit = round_down(Fraction(order.limit_price) * factor, self._tick(position))
            again = replace(order, id=uuid.uuid4(), limit_price=limit, shares=position.shares)
            self._place(again)
            remaining.append(again)
        return remaining

    def _factor(self, instrument: str) -> tuple[Fraction, Decimal] | None:
        """The Adjustment Factor of the splits and bonuses ex-dated in the Instrument since the
        account's last trading day, and its last close then in the day's prices; None if
        there are none."""
        previous = _previous_session(self._calendar, self._day)
        actions = self._data.corporate_actions(instrument, start=previous + timedelta(days=1))
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
        stop, target = position.stop, position.target
        restated = replace(
            position,
            shares=shares,
            entry=paisa(Fraction(position.entry) * factor),
            stop=None if stop is None else round_down(Fraction(stop) * factor, tick),
            target=None if target is None else round_down(Fraction(target) * factor, tick),
            as_of=self._day,
        )
        self._positions[position.id] = restated
        self._write_position(
            restated,
            event="restated",
            factor=factor,
            cash_in_lieu=paisa((exact - shares) * Fraction(close)),
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
        """One sell Order for the next session for each open Position with exits, or with a
        sale the open did not fill; the number placed. A portfolio Strategy's holding has
        no exit of its own and rests with no Order."""
        placed = 0
        for position in self.open_positions():
            reason = self._exits_due.get(position.id)
            bar = self._bar(position.instrument)
            if (
                reason is None
                and bar is not None
                and position.stop is not None
                and position.time_stop_days is not None
            ):
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
                if position.target is None:
                    continue
                limit = position.target
            else:
                limit = self._exit_limit(position, band=band, tick_sizes=tick_sizes)
            self._sell(position, reason or "target", limit, run_id=run_id, session=session)
            placed += 1
        return placed

    def place_sales(
        self,
        leavers: Sequence[str],
        *,
        run_id: uuid.UUID,
        session: date,
        band: Decimal,
        tick_sizes: TickSizes,
    ) -> int:
        """A Rebalance's sell for the next session, at the close less the band, for each
        open Position in a leaving Instrument not already given its sell; the number
        placed."""
        placed = 0
        leaving = set(leavers)
        for position in self.open_positions():
            if position.instrument not in leaving or position.id in self._selling:
                continue
            limit = self._exit_limit(position, band=band, tick_sizes=tick_sizes)
            self._sell(position, "rebalance", limit, run_id=run_id, session=session)
            placed += 1
        return placed

    def _exit_limit(
        self, position: queries.LedgerPosition, *, band: Decimal, tick_sizes: TickSizes
    ) -> Decimal:
        tick = tick_sizes.tick(
            self._day, reference_close=self._data.reference_close(position.instrument)
        )
        return self._fill_model.exit_limit(self.close(position.instrument), band=band, tick=tick)

    def _sell(
        self,
        position: queries.LedgerPosition,
        purpose: OrderPurpose,
        limit: Decimal,
        *,
        run_id: uuid.UUID,
        session: date,
    ) -> None:
        self._place(
            ledger.Order(
                uuid.uuid4(),
                run_id,
                None,
                position.id,
                position.instrument,
                position.symbol,
                purpose,
                limit,
                position.shares,
                session,
            )
        )
        self._selling.add(position.id)

    def entry_limit(self, instrument: str, *, band: Decimal) -> Decimal:
        """A Rebalance buy's limit for the next session: the day's close plus the band,
        rounded up to the tick in force."""
        tick = self._tick_sizes.tick(
            self._day, reference_close=self._data.reference_close(instrument)
        )
        return self._fill_model.entry_limit(self.close(instrument), band=band, tick=tick)

    def affordable(self, instrument: str, budget: Decimal, *, limit: Decimal, session: date) -> int:
        """The most shares a buy at ``limit`` on ``session`` takes with its charges within
        ``budget``; none for a budget of nothing."""
        if budget <= 0:
            return 0
        shares = math.floor(budget / limit)
        average = self._data.average_traded_value(instrument, TRADED_VALUE_DAYS)
        while shares > 0:
            charges = self._costs.charges(
                "buy", day=session, price=limit, shares=shares, average_traded_value=average
            )
            if shares * limit + charges.total <= budget:
                break
            shares -= 1
        return shares

    def boundary_exits(self) -> int:
        """Close every open Position at the day's close (the last close before it, on a day
        its Instrument did not trade), after costs: each resting target cancelled, reason
        ``boundary``, and a ``boundary`` sell filled at once. The number of Fills."""
        for target in self._targets.values():
            self._cancel(target, "boundary")
        self._targets = {}
        positions = self.open_positions()
        for position in positions:
            outcome = self._fill_model.boundary(
                self._bar(position.instrument), last_close=self.close(position.instrument)
            )
            order = ledger.Order(
                uuid.uuid4(),
                position.run_id,
                None,
                position.id,
                position.instrument,
                position.symbol,
                "boundary",
                outcome.price,
                position.shares,
                self._day,
            )
            self._place(order)
            self._fill(order, outcome.price)
            self._close_position(position.id, price=outcome.price, reason="boundary")
        return len(positions)

    def place_entries(self, passes: Sequence[Pass], *, run_id: uuid.UUID, session: date) -> int:
        """A buy for the next session at each Pass's entry limit; the number placed."""
        for granted in passes:
            verify_pass(granted, granted.plan, run_id=run_id)
            plan = granted.plan
            self._place(
                ledger.Order(
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

    def _meet_entry(self, order: ledger.Order) -> int:
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
        if granted.target is not None:
            # The target rests with the broker from the Fill.
            target = replace(
                order, id=uuid.uuid4(), rank=None, purpose="target", limit_price=granted.target
            )
            self._place(target)
            self._targets[position.id] = target
        return 1

    def _meet_exit(self, order: ledger.Order) -> int:
        """A stop or time-stop exit, or a Rebalance's sale, against the open (a Boundary
        Exit never rests)."""
        purpose = order.purpose
        if purpose != "stop" and purpose != "time" and purpose != "rebalance":
            raise ValueError(
                f"order {order.id} is a {purpose} order, not a stop, time or Rebalance exit"
            )
        outcome = self._fill_model.exit(order.limit_price, self._bar(order.instrument))
        if isinstance(outcome, Unfilled):
            self._cancel(order, outcome.reason)
            self._exits_due[order.position_id] = purpose
            return 0
        self._fill(order, outcome.price)
        self._close_position(order.position_id, price=outcome.price, reason=purpose)
        return 1

    def _meet_target(self, order: ledger.Order) -> int:
        position = self._positions[order.position_id]
        # Only a Position with exits has a target.
        assert position.stop is not None
        outcome = self._fill_model.target(
            order.limit_price, self._bar(order.instrument), stop=position.stop
        )
        if isinstance(outcome, Unfilled):
            return 0
        del self._targets[order.position_id]
        self._fill(order, outcome.price)
        self._close_position(order.position_id, price=outcome.price, reason="target")
        return 1

    def _close_position(
        self, position_id: uuid.UUID, *, price: Decimal, reason: ExitReason
    ) -> None:
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

    def _write_order(self, order: ledger.Order, *, event: str, reason: str | None) -> None:
        ledger.insert_order(
            self._conn,
            self._account,
            order,
            event=event,
            day=self._day,
            seq=self._next_seq(),
            reason=reason,
        )

    def _place(self, order: ledger.Order) -> None:
        self._write_order(order, event="placed", reason=None)

    def _cancel(self, order: ledger.Order, reason: str) -> None:
        self._write_order(order, event="cancelled", reason=reason)

    def _fill(self, order: ledger.Order, price: Decimal) -> None:
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
        ledger.insert_fill(
            self._conn,
            self._account,
            order,
            price=price,
            charges=charges,
            day=self._day,
            seq=self._next_seq(),
        )

    def _write_position(
        self,
        position: queries.LedgerPosition,
        *,
        event: str | None = None,
        factor: Fraction | None = None,
        cash_in_lieu: Decimal | None = None,
    ) -> None:
        ledger.insert_position(
            self._conn,
            self._account,
            position,
            day=self._day,
            seq=self._next_seq(),
            event=event,
            factor=factor,
            cash_in_lieu=cash_in_lieu,
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


def _portfolio_step(
    conn: psycopg.Connection,
    strategy: PortfolioStrategy,
    calendar: TradingCalendar,
    account: queries.Account,
    *,
    as_of: date,
    next_session: date,
    initial_buy: bool,
    entries: bool,
) -> str | None:
    """What a portfolio Strategy's Run as of ``as_of`` does: its account's Initial Buy; a
    Rebalance, on the last trading day of a Rebalance month; the Rebalance's buys, on the
    trading day after the account Rebalanced; or nothing. A Run that places no entries
    (``entries`` false) does nothing."""
    if initial_buy:
        return "initial_buy"
    if not entries:
        return None
    months = strategy.rebalance_months
    if as_of.month in months and next_session.month != as_of.month:
        return "rebalance"
    previous = _previous_session(calendar, as_of)
    if previous.month in months and as_of.month != previous.month:
        decided = queries.runs(conn, **account, as_of=previous)
        if any(r.portfolio_step == "rebalance" for r in decided):
            return "rebalance_buys"
    return None


def _portfolio_decision(
    conn: psycopg.Connection,
    strategy: PortfolioStrategy,
    step: str | None,
    held: Sequence[queries.LedgerPosition],
    members: Sequence[Member],
    data: StoredMarketData,
    calendar: TradingCalendar,
    account: queries.Account,
    *,
    as_of: date,
) -> tuple[list[Candidate], tuple[str, ...]]:
    """The Run's Candidates (the entrants, best first) and the Instruments it sells. A
    Rebalance asks the Strategy; an Initial Buy asks it with nothing held; the Rebalance's
    buys are the entrants its Run on the trading day before recorded."""
    if step == "initial_buy":
        if held:
            raise ValueError(
                f"the account holds {len(held)} Positions; an Initial Buy starts empty"
            )
        return list(strategy.rebalance((), members, data, as_of).entrants), ()
    if step == "rebalance":
        holdings = [Member(p.instrument, p.symbol) for p in held]
        decided = strategy.rebalance(holdings, members, data, as_of)
        return list(decided.entrants), decided.leavers
    if step == "rebalance_buys":
        previous = _previous_session(calendar, as_of)
        (rebalance,) = [
            r
            for r in queries.runs(conn, **account, as_of=previous)
            if r.portfolio_step == "rebalance"
        ]
        return [
            Candidate(c.rank, c.instrument, c.symbol, c.score, c.facts)
            for c in queries.candidates(conn, rebalance.id)
        ], ()
    return [], ()


def _split_cash(
    entrants: Sequence[Candidate],
    *,
    cash: Decimal,
    account: _Account,
    band: Decimal,
    session: date,
) -> Iterator[PortfolioPlan]:
    """A portfolio Plan for each entrant, in rank order: the cash held now split equally
    between them, each share spent on as many shares as it buys at the limit (the day's
    close plus the band, rounded up to the tick) with the buy's charges. The Risk Engine
    then caps each at the per-Position Limit; what no Plan spends stays in cash."""
    if not entrants:
        return
    share = cash / len(entrants)
    for entrant in entrants:
        limit = account.entry_limit(entrant.instrument, band=band)
        shares = account.affordable(entrant.instrument, share, limit=limit, session=session)
        yield PortfolioPlan(entrant.rank, entrant.instrument, entrant.symbol, limit, shares)


def _proposals(
    plans: Sequence[Plan | PortfolioPlan],
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
