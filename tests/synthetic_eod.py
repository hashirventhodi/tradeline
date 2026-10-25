"""Deterministic synthetic NSE end-of-day files, written as playback fixtures.

Phase 2's test dataset (#72): a few Instruments over two to three years whose
prices are stated, not recorded, so a test can say exactly which day a Strategy
surfaces a Candidate and what its Plan is. ``write_fixtures`` writes, for every
calendar day of a ``Market``, the bhavcopy NSE would serve (a 404 on a day the
Trading Calendar has closed), the equity master, Kite's instrument dump, and
NSE's corporate-action lists for each calendar year, and NSE's NIFTY Total
Market list with each Stock's sector, as the JSON files ``PlaybackTransport``
serves. ``load_market`` then loads them through
``ingest eod`` and ``ingest corporate-actions`` playback, the way real data
arrives, declaring the Market's Universe Rule in force when the Market says
when (``RuleDeclaration``, ADR 0005's Rule Validity), and
``compute_universe_months`` stores the Universe under that rule.
``load_sectors_on`` loads the sectors as NSE's list read on a given day.
``freeze_protocol`` stores the Universe over a range and freezes a Protocol
whose Windows span it, at the commit the tests run at, for a backtest there.

A ``Stock`` is described by its path, the prices NSE would publish if it had
no Corporate Actions: a starting close, a slope (the close moves by it every
trading day), and a half range (each bar's high and low sit that far outside
its open and close). Each day opens at the previous close. ``events`` change
that path from a day on:

- ``Trend``: a new slope from that day.
- ``Dip``: the day's low reaches ``depth`` below its close (a pullback wick).
- ``Gap``: the day opens ``amount`` away from the previous close and the path
  continues from there (a gap past a stop is a large negative ``Gap``).
- ``CircuitLock``: the day is pinned at the previous close plus ``amount``,
  open, high, low and close equal (GLOSSARY: Circuit Lock).
- ``Dividend``: a dividend NSE lists, by its subject; it leaves the path as it is.
- ``Rights``: a rights issue NSE lists, by its subject; it leaves the path as it is.
- ``Split`` and ``Bonus``: a Corporate Action. From its Ex-Date NSE publishes
  the path times the factor (a Rs 10 to Rs 5 split halves the price and
  doubles the volume), so adding an action changes no bar before its Ex-Date.
  Read as of a day on or after the Ex-Date, the Adjusted Series is the path
  times the factor throughout; read as of a day before it, the path itself.
  An action whose published prices fall off the 5 paisa tick, or whose volume
  is fractional, is refused, so every published price is one a test can state.

Every price is stated in rupees as a ``Decimal`` on the 5 paisa tick. To
extend the dataset, add a ``Stock`` or an event to ``base_market`` (or derive a
variant with ``Market.with_events``) and state the new expected values in the
test from the description, never from the generator's output.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg

from checkout import TEST_COMMIT
from limits import PULLBACK
from tradeline.benchmark import load_benchmark_funds
from tradeline.config import UniverseRule
from tradeline.costs import CostModel, Slippage, load_cost_schedule
from tradeline.dates import IST, calendar_days, month_of, next_month
from tradeline.demergers import load_demergers
from tradeline.freeze import FreezeResult
from tradeline.freeze import freeze_protocol as freeze
from tradeline.holdout_checks import TaxTestOutcome, check_holdout
from tradeline.ingest.benchmark import ingest_benchmark
from tradeline.ingest.corporate_actions import ingest_corporate_actions, year_chunks
from tradeline.ingest.demergers import ingest_demergers
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.index_history import ingest_index_history
from tradeline.metrics import HaltLimits
from tradeline.protocol import Gate, Layout, Terms, months_between
from tradeline.risk import Limits
from tradeline.sectors import load_sectors
from tradeline.sources import amfi, kite, nse
from tradeline.strategy import Strategy
from tradeline.strategy.pullback import PullbackV1
from tradeline.tax import load_tax_schedule
from tradeline.trading_calendar import TradingCalendar, load_trading_calendar
from tradeline.transport import PlaybackTransport, RecordingTransport, Request, Response
from tradeline.universe import compute_universe, declare_rule_validity

REPO_ROOT = Path(__file__).resolve().parents[1]
TICK = Decimal("0.05")


def repo_calendar() -> TradingCalendar:
    return load_trading_calendar(REPO_ROOT / "trading_calendar.csv")


# The date whose cost schedule prices a test backtest's trades, fixed so results never
# move with the calendar.
COSTS_AS_OF = date(2026, 10, 24)
# The repo's [protocol] starting Equity and exit band (tradeline.toml).
STARTING_EQUITY = Decimal("500000")
# The factor fund's AMFI code (tradeline.toml's [benchmark]).
FACTOR_FUND = 141596
# The Benchmark's AMFI code (benchmark_funds.csv).
BENCHMARK_SCHEME = 147625
EXIT_BAND = Decimal("0.05")


def repo_costs(rates_as_of: date = COSTS_AS_OF) -> CostModel:
    """The checked-in cost schedule as of ``rates_as_of``, with the default Slippage."""
    return CostModel(load_cost_schedule(REPO_ROOT / "cost_rates.csv"), Slippage(), rates_as_of)


# --- Events -------------------------------------------------------------------


@dataclass(frozen=True)
class Trend:
    day: date
    slope: Decimal


@dataclass(frozen=True)
class Dip:
    day: date
    depth: Decimal


@dataclass(frozen=True)
class Gap:
    day: date
    amount: Decimal


@dataclass(frozen=True)
class CircuitLock:
    day: date
    amount: Decimal


@dataclass(frozen=True)
class Split:
    """A face-value split, e.g. Rs 10 to Rs 5 (factor 1/2)."""

    ex_date: date
    from_face: int
    to_face: int

    @property
    def factor(self) -> Fraction:
        return Fraction(self.to_face, self.from_face)

    @property
    def subject(self) -> str:
        return f"Face Value Split From Rs {self.from_face} To Rs {self.to_face}"


@dataclass(frozen=True)
class Bonus:
    """A bonus of ``new`` shares for every ``held``, e.g. 1:1 (factor 1/2)."""

    ex_date: date
    new: int
    held: int

    @property
    def factor(self) -> Fraction:
        return Fraction(self.held, self.held + self.new)

    @property
    def subject(self) -> str:
        return f"Bonus {self.new}:{self.held}"


@dataclass(frozen=True)
class Absent:
    """No row for the Stock in NSE's files from ``first`` to ``last`` (one day when ``last``
    is None): a day it did not trade, or a suspension. Its path runs on underneath, and
    the next published day opens at the path's previous close."""

    first: date
    last: date | None = None

    def covers(self, day: date) -> bool:
        return self.first <= day <= (self.last or self.first)


@dataclass(frozen=True)
class Volume:
    """The shares a day on the path from ``day`` on."""

    day: date
    volume: int


@dataclass(frozen=True)
class Dividend:
    """A dividend as NSE lists it: its Ex-Date, its subject, and the face value NSE stores
    with it. The path is unchanged by it (no fall by the amount), so the only money it
    moves is the cash a holder is credited."""

    ex_date: date
    subject: str
    face_value: int = 10


@dataclass(frozen=True)
class Rights:
    """A rights issue as NSE lists it, by its subject. The path is unchanged by it; a test
    states the Ex-Date's fall with a ``Gap``."""

    ex_date: date
    subject: str


@dataclass(frozen=True)
class Demerger:
    """A demerger as NSE lists it, by its subject. The path is unchanged by it; a test
    gives the child its own Stock and the demerger table its row."""

    ex_date: date
    subject: str = "Demerger"


PathEvent = Trend | Dip | Gap | CircuitLock | Volume
Action = Split | Bonus
Event = PathEvent | Action | Absent | Dividend | Rights | Demerger


# --- Stocks and the Market ----------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One synthetic bar: on the path, or as NSE publishes it after Corporate Actions."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Stock:
    symbol: str
    isin: str
    start: Decimal  # the close before the Market's first trading day
    slope: Decimal
    half_range: Decimal
    volume: int  # shares a day on the path
    listed: date = date(2010, 1, 4)
    series: str = "EQ"
    events: tuple[Event, ...] = ()
    # NSE's sector (the Total Market list's Industry); None: not on the list.
    sector: str | None = None
    # A zigzag on the path: on the Market's even-numbered trading days (the first is 0)
    # the close moves by the slope plus the swing, on odd ones by the slope less it.
    swing: Decimal = Decimal(0)

    def absent(self, day: date) -> bool:
        """No row is published for the Stock on ``day``."""
        return any(e.covers(day) for e in self.events if isinstance(e, Absent))

    @property
    def actions(self) -> list[Action]:
        return sorted(
            (e for e in self.events if isinstance(e, Split | Bonus)), key=lambda a: a.ex_date
        )

    @property
    def dividends(self) -> list[Dividend]:
        return sorted((e for e in self.events if isinstance(e, Dividend)), key=lambda d: d.ex_date)

    @property
    def rights(self) -> list[Rights]:
        return sorted((e for e in self.events if isinstance(e, Rights)), key=lambda r: r.ex_date)

    @property
    def demergers(self) -> list[Demerger]:
        return sorted((e for e in self.events if isinstance(e, Demerger)), key=lambda d: d.ex_date)

    def path_bars(self, days: Sequence[date]) -> list[Bar]:
        """The path, one bar per trading day: NSE's prices had there been no action."""
        by_day: dict[date, list[PathEvent]] = {}
        for event in self.events:
            if isinstance(event, Trend | Dip | Gap | CircuitLock | Volume):
                by_day.setdefault(event.day, []).append(event)
        slope, previous, bars, volume = self.slope, self.start, [], self.volume
        for index, day in enumerate(days):
            today = by_day.get(day, [])
            for event in today:
                if isinstance(event, Trend):
                    slope = event.slope
                elif isinstance(event, Volume):
                    volume = event.volume
            move = slope + (self.swing if index % 2 == 0 else -self.swing)
            gap = sum((e.amount for e in today if isinstance(e, Gap)), Decimal(0))
            lock = next((e for e in today if isinstance(e, CircuitLock)), None)
            if lock is not None:
                price = previous + lock.amount
                bar = Bar(day, price, price, price, price, volume)
            else:
                open_ = previous + gap
                close = open_ + move
                low = min(open_, close) - self.half_range
                for event in today:
                    if isinstance(event, Dip):
                        low = min(low, close - event.depth)
                high = max(open_, close) + self.half_range
                bar = Bar(day, open_, high, low, close, volume)
            if bar.low <= 0:
                raise ValueError(f"{self.symbol}'s path falls to {bar.low} on {day}")
            bars.append(bar)
            previous = bar.close
        return bars

    def raw_bars(self, days: Sequence[date]) -> list[Bar]:
        """The bars NSE publishes: from each Ex-Date, the path times the action's factor."""
        return [self._raw(bar) for bar in self.path_bars(days)]

    def _raw(self, bar: Bar) -> Bar:
        factor = Fraction(1)
        for action in self.actions:
            if action.ex_date <= bar.day:
                factor *= action.factor

        def price(value: Decimal) -> Decimal:
            raw = Fraction(value) * factor
            ticks = raw / Fraction(TICK)
            if ticks.denominator != 1:
                raise ValueError(
                    f"{self.symbol}'s raw price {float(raw)} on {bar.day} is off the tick;"
                    " state a path the factor multiplies onto the tick"
                )
            return Decimal(ticks.numerator) * TICK

        volume = bar.volume / factor
        if volume.denominator != 1:
            raise ValueError(f"{self.symbol}'s raw volume on {bar.day} is fractional")
        return Bar(
            bar.day,
            price(bar.open),
            price(bar.high),
            price(bar.low),
            price(bar.close),
            volume.numerator,
        )


@dataclass(frozen=True)
class RuleDeclaration:
    """The Operator's declaration that the Market's rule is in force from a day."""

    in_force_from: date
    declared_at: datetime


@dataclass(frozen=True)
class Market:
    start: date
    end: date
    stocks: tuple[Stock, ...]
    rule: UniverseRule = field(default_factory=UniverseRule)
    # None: the rule is never declared, so every month under it is Reconstructed.
    declaration: RuleDeclaration | None = None

    def trading_days(self, calendar: TradingCalendar) -> list[date]:
        return list(calendar.trading_days(self.start, self.end))

    def stock(self, symbol: str) -> Stock:
        (found,) = (s for s in self.stocks if s.symbol == symbol)
        return found

    def with_events(self, symbol: str, *events: Event) -> Market:
        """This Market with ``events`` added to one Stock."""
        stocks = tuple(
            replace(s, events=s.events + events) if s.symbol == symbol else s for s in self.stocks
        )
        return replace(self, stocks=stocks)

    def with_stock(self, symbol: str, **changes: Any) -> Market:
        """This Market with one Stock's fields changed, e.g. its volume or sector."""
        stocks = tuple(replace(s, **changes) if s.symbol == symbol else s for s in self.stocks)
        return replace(self, stocks=stocks)


# --- Writing fixtures ---------------------------------------------------------


class _Served:
    """Answers each request from a fixed table; the inner transport of a recording."""

    def __init__(self, responses: dict[str, Response]) -> None:
        self._responses = responses

    def send(self, request: Request) -> Response:
        return self._responses[request.url]


def write_fixtures(market: Market, directory: Path, calendar: TradingCalendar) -> Path:
    """Write every response ``load_market`` asks for into ``directory``; returns it."""
    responses = dict(_responses(market, calendar))
    recorder = RecordingTransport(_Served(responses), directory)
    for url in responses:
        recorder.send(Request("GET", url))
    return directory


def _responses(market: Market, calendar: TradingCalendar) -> Iterator[tuple[str, Response]]:
    days = market.trading_days(calendar)
    raw = {s.symbol: {b.day: b for b in s.raw_bars(days)} for s in market.stocks}
    previous: dict[str, Decimal] = {}
    open_days = set(days)
    for day in calendar_days(market.start, market.end):
        if day not in open_days:
            yield nse.bhavcopy_url(day), Response(404, (), b"")
            continue
        lines = [",".join(_UDIFF_HEADER)]
        for stock in market.stocks:
            if stock.absent(day):
                continue
            bar = raw[stock.symbol][day]
            lines.append(_udiff_line(stock, bar, previous.get(stock.symbol, bar.open)))
            previous[stock.symbol] = bar.close
        yield nse.bhavcopy_url(day), _ok(_zip(f"{day:%Y%m%d}.csv", "\n".join(lines) + "\n"))
    yield nse.EQUITY_MASTER_URL, _ok(_equity_master(market.stocks).encode())
    yield kite.INSTRUMENTS_URL, _ok(_kite_dump(market.stocks).encode())
    yield f"{nse.SITE_HOST}/", Response(200, (("set-cookie", "nsit=synthetic; Path=/"),), b"")
    yield (
        nse.index_constituents_url(nse.TOTAL_MARKET_LIST),
        _ok(_total_market_list(market.stocks).encode()),
    )
    for chunk_start, chunk_end in year_chunks(market.start, market.end):
        for nse_list in nse.CORPORATE_ACTION_LISTS:
            records = (
                _corporate_actions(market.stocks, chunk_start, chunk_end)
                if nse_list == "equities"
                else []
            )
            url = nse.corporate_actions_url(chunk_start, chunk_end, nse_list=nse_list)
            yield url, _ok(json.dumps(records).encode())


_UDIFF_HEADER = (
    "TradDt",
    "TckrSymb",
    "SctySrs",
    "ISIN",
    "OpnPric",
    "HghPric",
    "LwPric",
    "ClsPric",
    "LastPric",
    "PrvsClsgPric",
    "TtlTradgVol",
    "TtlTrfVal",
    "TtlNbOfTxsExctd",
)


def _udiff_line(stock: Stock, bar: Bar, previous_close: Decimal) -> str:
    values = (
        bar.day.isoformat(),
        stock.symbol,
        stock.series,
        stock.isin,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.close,
        previous_close,
        bar.volume,
        bar.close * bar.volume,
        1000,
    )
    return ",".join(str(v) for v in values)


def _equity_master(stocks: Iterable[Stock]) -> str:
    lines = ["SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, ISIN NUMBER"]
    for s in stocks:
        lines.append(f"{s.symbol},{s.symbol} Limited,{s.series},{s.listed:%d-%b-%Y},{s.isin}")
    return "\n".join(lines) + "\n"


def _total_market_list(stocks: Iterable[Stock]) -> str:
    lines = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    for s in stocks:
        if s.sector is not None:
            lines.append(f"{s.symbol} Limited,{s.sector},{s.symbol},{s.series},{s.isin}")
    return "\n".join(lines) + "\n"


def _kite_dump(stocks: Sequence[Stock]) -> str:
    lines = ["instrument_token,tradingsymbol,instrument_type,segment"]
    for token, s in enumerate(stocks, start=1001):
        lines.append(f"{token},{kite.trading_symbol(s.symbol, s.series)},EQ,NSE")
    return "\n".join(lines) + "\n"


def _corporate_actions(stocks: Iterable[Stock], start: date, end: date) -> list[dict[str, str]]:
    return [
        {
            "symbol": s.symbol,
            "series": s.series,
            "isin": s.isin,
            "exDate": f"{a.ex_date:%d-%b-%Y}",
            "recDate": f"{a.ex_date:%d-%b-%Y}",
            "faceVal": str(a.face_value) if isinstance(a, Dividend) else "10",
            "subject": a.subject,
        }
        for s in stocks
        for a in (*s.actions, *s.dividends, *s.rights, *s.demergers)
        if start <= a.ex_date <= end
    ]


def _ok(body: bytes) -> Response:
    return Response(200, (), body)


def _zip(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


# --- Loading ------------------------------------------------------------------


def load_market(conn: psycopg.Connection, fixtures: Path, market: Market) -> None:
    """Load the Market's fixtures through ``ingest eod`` and ``ingest corporate-actions``,
    then declare its rule in force if the Market declares it."""
    ingest_eod(conn, PlaybackTransport(fixtures), start=market.start, end=market.end)
    ingest_corporate_actions(conn, PlaybackTransport(fixtures), start=market.start, end=market.end)
    if market.declaration is not None:
        declare_rule_validity(
            conn,
            market.rule.version,
            in_force_from=market.declaration.in_force_from,
            declared_at=market.declaration.declared_at,
            rule=market.rule,
        )


def load_sectors_on(conn: psycopg.Connection, fixtures: Path, day: date) -> None:
    """Load each Stock's sector as NSE's Total Market list read on ``day``."""
    load_sectors(conn, PlaybackTransport(fixtures), effective_date=day)


def compute_universe_months(
    conn: psycopg.Connection, market: Market, first: date, last: date
) -> None:
    """Store the Universe under the Market's rule for each month from ``first`` to ``last``."""
    month = month_of(first)
    while month <= last:
        compute_universe(conn, market.rule, month=month)
        month = next_month(month)


# The spec's grid (#72).
GRID = {
    "k": (Decimal("1.5"), Decimal("2"), Decimal("2.5"), Decimal("3")),
    "r": (Decimal("1.5"), Decimal("2"), Decimal("3")),
    "n": (Decimal(5), Decimal(10), Decimal(15)),
}


def protocol_terms(first: date, last: date) -> Terms:
    """The spec's Terms with one Window spanning the months of ``first`` to ``last``: the
    Holdout the last month, the Out-of-Sample Window the month before, and every month
    before that In-Sample (at least one)."""
    months = len(months_between(first, last))
    if months < 3:
        raise ValueError(
            "a Protocol's Windows need three months: In-Sample, Out-of-Sample, Holdout"
        )
    return Terms(
        grid=GRID,
        min_in_sample_trades=30,
        layout=Layout(
            holdout_start=month_of(last),
            holdout_months=1,
            windows=1,
            out_of_sample_months=1,
            in_sample_months=months - 2,
        ),
        band=EXIT_BAND,
        starting_equity=STARTING_EQUITY,
        risk_free_rate=Decimal("0.065"),
        gate=Gate(
            sharpe_above=Decimal("1.0"),
            drawdown_below=Decimal("0.15"),
            profit_factor_above=Decimal("1.3"),
            min_stitched_trades=150,
        ),
        warm_up_trading_days=250,
    )


def freeze_protocol(
    conn: psycopg.Connection,
    market: Market,
    first: date,
    last: date,
    *,
    terms: Terms | None = None,
    strategy: Strategy | None = None,
    limits: Limits | None = None,
    halts: HaltLimits | None = None,
    commit: str | None = None,
) -> FreezeResult:
    """Store the Universe for the months of ``first`` to ``last`` and freeze a Protocol over
    them (``protocol_terms`` unless ``terms`` is given) for ``pullback_v1`` under the
    Market's rule, the default Limits, Halts and Slippage and the checked-in schedule as of
    ``COSTS_AS_OF``, at ``TEST_COMMIT`` (the commit the tests run at) unless ``commit``
    is given."""
    compute_universe_months(conn, market, first, last)
    return freeze(
        conn,
        terms or protocol_terms(first, last),
        strategy=strategy or PullbackV1(),
        rule_version=market.rule.version,
        limits=limits or PULLBACK,
        halts=halts or HaltLimits(daily_loss=D("0.03"), drawdown=D("0.10")),
        schedule=load_cost_schedule(REPO_ROOT / "cost_rates.csv"),
        slippage=Slippage(),
        costs_as_of_for_tests=COSTS_AS_OF,
        calendar=repo_calendar(),
        commit=commit or TEST_COMMIT,
        tax_schedule=load_tax_schedule(REPO_ROOT / "tax_rates.csv"),
        benchmark_funds=load_benchmark_funds(REPO_ROOT / "benchmark_funds.csv"),
        factor_fund=FACTOR_FUND,
    )


# --- The base Market ----------------------------------------------------------

D = Decimal
# The Run day #74's tests ask about; every Dip that makes a pullback is on it.
RUN_DAY = date(2023, 6, 15)

BASE_RULE = UniverseRule(
    top_n=5, lookback_trading_days=20, min_listing_trading_days=20, min_presence_percent=90
)


def base_market() -> Market:
    """Six EQ stocks from 2022-01-01 to 2024-06-30.

    - GAMMA and ALPHA share a path, so their 126-day returns tie; both dip
      below their 20-day average on ``RUN_DAY``. ALPHA splits Rs 10 to Rs 5 in
      2022-09, so it is read on ``RUN_DAY`` at half GAMMA's prices. GAMMA gaps
      60 rupees down in 2023-07, past any stop a Plan on ``RUN_DAY`` would set;
      ALPHA is circuit-locked in 2023-07.
    - BETA rises from a higher price, so its 126-day return is lower; it dips on
      ``RUN_DAY`` and has a 1:1 bonus in 2024-02.
    - DELTA rises without dipping. EPSILON falls (below its 200-day average)
      and dips. OMEGA dips but trades too little to be in the Universe.

    ``BASE_RULE`` is declared in force from the first day, before it, so every
    month computed under it is a Recorded Universe.
    """
    start = date(2022, 1, 1)
    return Market(
        start=start,
        end=date(2024, 6, 30),
        rule=BASE_RULE,
        declaration=RuleDeclaration(start, datetime.combine(start, time(), IST)),
        stocks=(
            Stock(
                "GAMMA",
                "INE000G01014",
                start=D("200.00"),
                slope=D("1.00"),
                half_range=D("2.00"),
                volume=100_000,
                events=(Dip(RUN_DAY, D("17.00")), Gap(date(2023, 7, 3), D("-60.00"))),
            ),
            Stock(
                "ALPHA",
                "INE000A01016",
                start=D("200.00"),
                slope=D("1.00"),
                half_range=D("2.00"),
                volume=100_000,
                events=(
                    Split(date(2022, 9, 15), from_face=10, to_face=5),
                    Dip(RUN_DAY, D("17.00")),
                    CircuitLock(date(2023, 7, 10), D("-30.00")),
                ),
            ),
            Stock(
                "BETA",
                "INE000B01018",
                start=D("300.00"),
                slope=D("0.50"),
                half_range=D("1.00"),
                volume=150_000,
                events=(Dip(RUN_DAY, D("8.50")), Bonus(date(2024, 2, 15), new=1, held=1)),
            ),
            Stock(
                "DELTA",
                "INE000D01010",
                start=D("150.00"),
                slope=D("0.50"),
                half_range=D("1.00"),
                volume=150_000,
            ),
            Stock(
                "EPSILON",
                "INE000E01012",
                start=D("500.00"),
                slope=D("-0.25"),
                half_range=D("1.00"),
                volume=100_000,
                events=(Dip(RUN_DAY, D("10.00")),),
            ),
            Stock(
                "OMEGA",
                "INE000O01016",
                start=D("100.00"),
                slope=D("0.50"),
                half_range=D("1.00"),
                volume=1_000,
                events=(Dip(RUN_DAY, D("8.50")),),
            ),
        ),
    )


# --- The two-year Market ------------------------------------------------------

# The two years #77's backtest runs over; the Market starts a year earlier, so the
# 200-day average has its bars on the first day.
TWO_YEARS = (date(2022, 7, 1), date(2024, 6, 28))


def two_year_market() -> Market:
    """Three EQ stocks from 2021-07-01 to 2024-06-30, each rising 1.00 a day on a half range
    of 2.00 from a different start, so its close on the Market's i-th trading day is its
    start plus i until an event moves it. Each pullback is a ``Dip`` of 17.00, which makes
    ATR(14) 6.00 (thirteen true ranges of 5.00 and the dip's 19.00), so ``pullback_v1``
    at k = 2, r = 2 plans a stop 12.00 under the close and a target 24.00 over it.
    What follows each pullback is one exit, or one cancelled entry:

    - ASTER (from 400): a pullback on 2022-08-03 held to its time stop; one on 2023-03-01
      whose next bar but one rises 30.00 and reaches both stop and target; one on
      2024-01-03 that gaps 15.00 down through its stop and then 60.00 down past the
      exit's band, falling 1.00 a day from then.
    - BIRCH (from 300): a pullback on 2022-11-01 followed by a rise of 2.75 a day for
      nine days, whose high equals the target on the eighth and passes it on the ninth;
      one on 2023-05-02 whose next day opens 5.00 above the close; one on 2024-03-12
      locked 20.00 down at the stop on its next bar but one, locked again the day after,
      then falling 1.00 a day.
    - CEDAR (from 200): a pullback on 2023-01-02 whose next bar but one gaps 30.00 above
      the target.

    No action is ex-dated, and no Stock has a sector.
    """
    start = date(2021, 7, 1)

    def stock(symbol: str, isin: str, price: str, *events: Event) -> Stock:
        return Stock(
            symbol,
            isin,
            start=D(price),
            slope=D("1.00"),
            half_range=D("2.00"),
            volume=100_000,
            events=events,
        )

    pullback = D("17.00")
    return Market(
        start=start,
        end=date(2024, 6, 30),
        rule=BASE_RULE,
        declaration=RuleDeclaration(start, datetime.combine(start, time(), IST)),
        stocks=(
            stock(
                "ASTER",
                "INE000S01011",
                "400.00",
                Dip(date(2022, 8, 3), pullback),
                Dip(date(2023, 3, 1), pullback),
                Trend(date(2023, 3, 3), D("30.00")),
                Dip(date(2023, 3, 3), D("44.00")),
                Trend(date(2023, 3, 6), D("1.00")),
                Dip(date(2024, 1, 3), pullback),
                Gap(date(2024, 1, 5), D("-15.00")),
                Gap(date(2024, 1, 8), D("-60.00")),
                Trend(date(2024, 1, 8), D("-1.00")),
            ),
            stock(
                "BIRCH",
                "INE000H01019",
                "300.00",
                Dip(date(2022, 11, 1), pullback),
                Trend(date(2022, 11, 2), D("2.75")),
                Trend(date(2022, 11, 16), D("1.00")),
                Dip(date(2023, 5, 2), pullback),
                Gap(date(2023, 5, 3), D("5.00")),
                Dip(date(2024, 3, 12), pullback),
                CircuitLock(date(2024, 3, 14), D("-20.00")),
                CircuitLock(date(2024, 3, 15), D("-1.00")),
                Trend(date(2024, 3, 18), D("-1.00")),
            ),
            stock(
                "CEDAR",
                "INE000C01014",
                "200.00",
                Dip(date(2023, 1, 2), pullback),
                Gap(date(2023, 1, 4), D("30.00")),
            ),
        ),
    )


# --- The Walk-Forward Market ----------------------------------------------------

# Its Protocol's Windows: three, each Out-of-Sample two months with the three before
# In-Sample, then a one-month Holdout.
WALK_FORWARD_LAYOUT = Layout(
    holdout_start=date(2023, 1, 1),
    holdout_months=1,
    windows=3,
    out_of_sample_months=2,
    in_sample_months=3,
)
# A small grid, so the fixture runs four cells a Window: k in {2, 3}, r in {2}, n in {5, 10}.
WALK_FORWARD_GRID = {
    "k": (Decimal("2"), Decimal("3")),
    "r": (Decimal("2"),),
    "n": (Decimal(5), Decimal(10)),
}
# The Protocol's range: the first In-Sample month to the Holdout's.
WALK_FORWARD_RANGE = (date(2022, 4, 1), date(2023, 1, 31))


def config_text(market: Market, extra_risk: str = "") -> str:
    """A ``tradeline.toml`` for commands over the Market: its rule, the checked-in Trading
    Calendar, tick sizes and cost schedule, and ``extra_risk`` lines under ``[risk]``."""
    rule = market.rule
    return (
        f"[universe]\ntop_n = {rule.top_n}\nlookback_trading_days = {rule.lookback_trading_days}\n"
        f"min_listing_trading_days = {rule.min_listing_trading_days}\n"
        f"min_presence_percent = {rule.min_presence_percent}\n"
        f'[integrity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[parity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[risk]\ntick_sizes = "{REPO_ROOT / "tick_sizes.csv"}"\n{extra_risk}'
        f'[costs]\nschedule = "{REPO_ROOT / "cost_rates.csv"}"\n'
        f'contract_notes = "{REPO_ROOT / "contract_notes.csv"}"\n'
    )


def walk_forward_terms() -> Terms:
    """The spec's Terms on ``WALK_FORWARD_LAYOUT`` and ``WALK_FORWARD_GRID``, a cell eligible
    from two in-sample trades, and no risk-free rate."""
    return replace(
        protocol_terms(*WALK_FORWARD_RANGE),
        grid=WALK_FORWARD_GRID,
        min_in_sample_trades=2,
        layout=WALK_FORWARD_LAYOUT,
        # Idle cash earns nothing, so at 6.5 percent a year a few trades in three months
        # all score far below zero, ranked by how long they hold, not by what they earn.
        risk_free_rate=Decimal(0),
    )


def walk_forward_market() -> Market:
    """Three EQ stocks from 2021-03-01 to 2023-01-31 for ``walk_forward_terms``' Windows
    (In-Sample 2022-04..06, 2022-06..08, 2022-08..10; Out-of-Sample 2022-07..08, 2022-09..10,
    2022-11..12; Holdout 2023-01). Each rises 1.00 a day on a half range of 2.00, and each
    pullback is a ``Dip`` of 17.00 (as in ``two_year_market``: ATR(14) 6.00, so at k = 2
    the stop is 12.00 under the close and at r = 2 the target 24.00 over it, out of reach
    in ten days). An entry fills at the next open, the pullback's close; a rise then
    carries it to its time stop, so n = 10 earns about twice what n = 5 does, unless the
    pullback is followed, seven trading days on, by a fall of 3.00 a day: n = 5 sells
    before the fall, n = 10 into it.

    - ASH (from 400): pullbacks on 2022-05-10 and 2022-07-12 (rises), 2022-09-13 (falls
      from 2022-09-22, rising again from 2022-09-29), and 2022-12-26, three trading days
      before the last out-of-sample day.
    - BIRCH (from 300): 2022-08-10 (rises) and 2022-10-11 (falls from 2022-10-20, rising
      again from 2022-10-28).
    - CEDAR (from 200): 2022-10-27, its Position open across the Window boundary on
      2022-10-31, and 2022-11-22.

    So the In-Sample Windows close one trade (2022-04..06: too few to choose), two
    (2022-06..08, both rises) and three (2022-08..10, two of them falls). No action is
    ex-dated, and no Stock has a sector.
    """
    start = date(2021, 3, 1)

    def stock(symbol: str, isin: str, price: str, *events: Event) -> Stock:
        return Stock(
            symbol,
            isin,
            start=D(price),
            slope=D("1.00"),
            half_range=D("2.00"),
            volume=100_000,
            events=events,
        )

    pullback = D("17.00")

    def fall(day: date, back: date) -> tuple[Event, Event]:
        return Trend(day, D("-3.00")), Trend(back, D("1.00"))

    return Market(
        start=start,
        end=date(2023, 1, 31),
        rule=BASE_RULE,
        declaration=RuleDeclaration(start, datetime.combine(start, time(), IST)),
        stocks=(
            stock(
                "ASH",
                "INE000A01016",
                "400.00",
                Dip(date(2022, 5, 10), pullback),
                Dip(date(2022, 7, 12), pullback),
                Dip(date(2022, 9, 13), pullback),
                *fall(date(2022, 9, 22), date(2022, 9, 29)),
                Dip(date(2022, 12, 26), pullback),
            ),
            stock(
                "BIRCH",
                "INE000B01014",
                "300.00",
                Dip(date(2022, 8, 10), pullback),
                Dip(date(2022, 10, 11), pullback),
                *fall(date(2022, 10, 20), date(2022, 10, 28)),
            ),
            stock(
                "CEDAR",
                "INE000C01014",
                "200.00",
                Dip(date(2022, 10, 27), pullback),
                Dip(date(2022, 11, 22), pullback),
            ),
        ),
    )


# --- The low-volatility Market ------------------------------------------------

# ``lowvol_v1``'s decision days on ``lowvol_market``: the Initial Buy's (the day before
# the account's first trading day, and a last trading day of March, which is no Rebalance
# for it), then the one Rebalance.
LOWVOL_INITIAL = date(2022, 3, 31)
LOWVOL_REBALANCE = date(2022, 9, 30)
# The Protocol's range: the one In-Sample month (March 2022) to the Holdout (2022-11).
LOWVOL_RANGE = (date(2022, 3, 1), date(2022, 11, 30))
LOWVOL_LAYOUT = Layout(
    holdout_start=date(2022, 11, 1),
    holdout_months=1,
    windows=7,
    out_of_sample_months=1,
    in_sample_months=1,
)
LOWVOL_RULE = UniverseRule(
    top_n=69, lookback_trading_days=20, min_listing_trading_days=20, min_presence_percent=90
)
# Each Stock's volume: 1,000,000 shares near Rs 100, a 20-day average traded value of
# about Rs 10 crore, so the Participation Cap (1 percent) never binds.
LOWVOL_VOLUME = 1_000_000


def lowvol_terms() -> Terms:
    """``lowvol_v1``'s one-cell Terms on ``LOWVOL_LAYOUT``: Out-of-Sample Windows each month
    from 2022-04 to 2022-10, the Holdout 2022-11, the band 1 percent."""
    return replace(
        protocol_terms(*LOWVOL_RANGE),
        grid={},
        min_in_sample_trades=0,
        layout=LOWVOL_LAYOUT,
        band=Decimal("0.01"),
        warm_up_trading_days=260,
    )


def lowvol_market(end: date = date(2022, 11, 30)) -> Market:
    """Seventy EQ stocks from 2021-03-01 to ``end`` (2022-11-30) for ``lowvol_v1``, each flat at
    Rs 100 with a ``swing``: its close alternates between 100 plus the swing and 100, so
    its 252 daily log returns are, in turn, plus and minus ln(1 + swing / 100), and the
    larger the swing the larger their standard deviation. Every Stock trades
    ``LOWVOL_VOLUME`` shares a day unless stated, and ``LOWVOL_RULE`` (the 69 most traded)
    is declared in force from the first day.

    - N01 to N33: swings 0.05 to 1.65 (N0k: 0.05 k), the least volatile, each without a
      row on 2021-07-01, inside the 253 trading days to ``LOWVOL_INITIAL`` (from
      2021-03-24) and before those to ``LOWVOL_REBALANCE`` (from 2021-09-27); N33 is
      suspended instead, from 2021-06-21 to 2021-07-02. So all 33 are ineligible on the
      Initial Buy's day and rank 1 to 33 at the Rebalance.
    - H01 to H30: swings 1.70 to 3.15 (H0j: 0.05 (33 + j)), ranks 1 to 30 on the Initial
      Buy's day. H15 has no row on 2022-07-15, so it is ineligible at the Rebalance. H20
      trades 1,000 shares a day from 2022-07-01, so it leaves the Universe from 2022-08,
      and gaps Rs 4,000 up on 2022-08-01. The other 28 rank 34 to 61 at the Rebalance:
      H29 60th, H30 61st.
    - O01 to O06: swings 3.15 (O01, tied with H30 and after it by Symbol: 31st on the
      Initial Buy's day, 62nd at the Rebalance) and 3.20 to 3.40.
    - Z: swing 5.00 and 500,000 shares a day, the least traded and out of the Universe
      until H20 trades less.
    """
    start = date(2021, 3, 1)

    def stock(symbol: str, swing: str, *events: Event, volume: int = LOWVOL_VOLUME) -> Stock:
        return Stock(
            symbol,
            f"INE{symbol:0>6}014",
            start=D("100.00"),
            slope=D(0),
            half_range=D("0.50"),
            volume=volume,
            swing=D(swing),
            events=events,
        )

    missing = Absent(date(2021, 7, 1))
    news = [stock(f"N{k:02}", str(D("0.05") * k), missing) for k in range(1, 33)]
    news.append(stock("N33", "1.65", Absent(date(2021, 6, 21), date(2021, 7, 2))))
    held: list[Stock] = []
    for j in range(1, 31):
        events: tuple[Event, ...] = ()
        if j == 15:
            events = (Absent(date(2022, 7, 15)),)
        elif j == 20:
            events = (Volume(date(2022, 7, 1), 1_000), Gap(date(2022, 8, 1), D("4000.00")))
        held.append(stock(f"H{j:02}", str(D("0.05") * (33 + j)), *events))
    others = [stock("O01", "3.15")]
    others += [stock(f"O{i:02}", str(D("3.15") + D("0.05") * (i - 1))) for i in range(2, 7)]
    return Market(
        start=start,
        end=end,
        rule=LOWVOL_RULE,
        declaration=RuleDeclaration(start, datetime.combine(start, time(), IST)),
        stocks=(*news, *held, *others, stock("Z", "5.00", volume=500_000)),
    )


# --- AMFI ---------------------------------------------------------------------

# AMFI's scheme master lines for the Benchmark (Direct Plan, Growth) and the factor fund.
AMFI_MASTER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
    "Net Asset Value;Date\r\n\r\nMotilal Oswal Mutual Fund\r\n\r\n"
    "147625;INF247L01957;-;Motilal Oswal Nifty 500 Index Fund;Direct Plan;Growth;{nav};{day}\r\n"
    "\r\nICICI Prudential Mutual Fund\r\n\r\n"
    "141596;INF109KC19U5;-;ICICI Prudential Nifty 100 Low Volatility 30 ETF;;;{nav};{day}\r\n"
)


def write_amfi_fixtures(
    navs: dict[int, dict[date, Decimal]], directory: Path, *, first: date, end: date
) -> Path:
    """Record AMFI's answers ``ingest benchmark`` asks for to ``end``, from ``first`` (the
    Benchmark's first date): the scheme master naming both schemes, and each scheme's
    NAV history window holding its NAVs in ``navs`` that fall inside it."""
    last = max(navs[BENCHMARK_SCHEME])
    master = AMFI_MASTER.format(nav=navs[BENCHMARK_SCHEME][last], day=f"{last:%d-%b-%Y}")
    responses = {amfi.SCHEME_MASTER_URL: _ok(master.encode())}
    for code, series in navs.items():
        for start, stop in amfi.nav_windows(first, end):
            records = [
                {"date": d.isoformat(), "nav": str(v), "upload_time": f"{d}T21:00:00.000Z"}
                for d, v in sorted(series.items())
                if start <= d <= stop
            ]
            body = {"data": {"nav_groups": [{"historical_records": records}]}}
            responses[amfi.nav_history_url(code, start, stop)] = _ok(json.dumps(body).encode())
    recorder = RecordingTransport(_Served(responses), directory)
    for url in responses:
        recorder.send(Request("GET", url))
    return directory


# --- The Benchmark and the index history (ADR 0020) ------------------------------

# The recorded niftyindices.com Total Return Index history, 2005-04 to 2019-09
# (fixtures/index_history/README.md).
INDEX_HISTORY_FIXTURES = Path(__file__).parent / "fixtures" / "index_history"
# Where ``lowvol_benchmark_navs`` starts rising, and by how much each trading day.
LOWVOL_BENCHMARK_RISE = date(2022, 8, 2)
LOWVOL_BENCHMARK_STEP = Decimal("2")


def lowvol_benchmark_navs(calendar: TradingCalendar) -> dict[date, Decimal]:
    """The Benchmark's synthetic NAV over ``lowvol_market``'s Protocol: Rs 100 on every
    trading day from 2022-03-01, then from ``LOWVOL_BENCHMARK_RISE`` up by
    ``LOWVOL_BENCHMARK_STEP`` each trading day, while the Strategy's flat stocks only swing
    (and H20 gaps up on 2022-08-01), so its Relative Drawdown deepens through September."""
    navs = {}
    nav = Decimal(100)
    for day in calendar.trading_days(date(2022, 3, 1), LOWVOL_RANGE[1]):
        if day >= LOWVOL_BENCHMARK_RISE:
            nav += LOWVOL_BENCHMARK_STEP
        navs[day] = nav
    return navs


def write_benchmark_fixtures(directory: Path, navs: dict[date, Decimal], *, end: date) -> Path:
    """AMFI's answers ``ingest benchmark`` asks for to ``end``: ``navs`` as the NAV history
    of both the Benchmark and the factor fund, from ``benchmark_funds.csv``'s first date."""
    funds = load_benchmark_funds(REPO_ROOT / "benchmark_funds.csv")
    return write_amfi_fixtures(
        {BENCHMARK_SCHEME: navs, FACTOR_FUND: navs}, directory, first=funds.first, end=end
    )


def load_benchmark(conn: psycopg.Connection, fixtures: Path, *, end: date) -> None:
    """Load AMFI's recorded answers through ``ingest benchmark``, never sleeping."""
    ingest_benchmark(
        conn,
        PlaybackTransport(fixtures),
        funds=load_benchmark_funds(REPO_ROOT / "benchmark_funds.csv"),
        factor_fund=FACTOR_FUND,
        end=end,
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
    )


# --- The pre-Holdout checks ---------------------------------------------------


def pass_holdout_checks(conn: psycopg.Connection, protocol: str) -> None:
    """Record every pre-Holdout check of ``protocol`` as passed at the commit the tests run
    at, for a Market with no unparsed dividend and no demerger: no contract note, accepted
    as unmet, and the tax-ledger tests taken as passed (``test_pre_holdout_checks`` runs
    them)."""
    result = check_holdout(
        conn,
        protocol=protocol,
        contract_notes=(),
        schedule=load_cost_schedule(REPO_ROOT / "cost_rates.csv"),
        listed=(),
        accept_no_contract_notes="a synthetic Market's test",
        tax_tests=lambda: TaxTestOutcome(True, "taken as passed"),
    )
    assert result.passed, result.summary()


def load_index_history(conn: psycopg.Connection) -> None:
    """Load the recorded index history through ``ingest index-history``."""
    ingest_index_history(conn, PlaybackTransport(INDEX_HISTORY_FIXTURES), sleep=lambda _s: None)


def load_demerger_table(conn: psycopg.Connection) -> None:
    """Store the checked-in demerger table through ``ingest_demergers``: a Protocol crediting
    demergers (``lowvol_v1``'s) freezes its hash, and a synthetic Market holds none of its
    parents."""
    ingest_demergers(conn, load_demergers(REPO_ROOT / "demergers.csv"))
