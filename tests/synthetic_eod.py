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
from tradeline.config import UniverseRule
from tradeline.costs import CostModel, Slippage, load_cost_schedule
from tradeline.dates import IST, calendar_days, month_of, next_month
from tradeline.freeze import FreezeResult
from tradeline.freeze import freeze_protocol as freeze
from tradeline.ingest.corporate_actions import ingest_corporate_actions, year_chunks
from tradeline.ingest.eod import ingest_eod
from tradeline.metrics import HaltLimits
from tradeline.protocol import Gate, Layout, Terms, months_between
from tradeline.risk import Limits
from tradeline.sectors import load_sectors
from tradeline.sources import kite, nse
from tradeline.strategy import Strategy
from tradeline.strategy.pullback import PullbackV1
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


PathEvent = Trend | Dip | Gap | CircuitLock
Action = Split | Bonus
Event = PathEvent | Action


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

    @property
    def actions(self) -> list[Action]:
        return sorted(
            (e for e in self.events if isinstance(e, Split | Bonus)), key=lambda a: a.ex_date
        )

    def path_bars(self, days: Sequence[date]) -> list[Bar]:
        """The path, one bar per trading day: NSE's prices had there been no action."""
        by_day: dict[date, list[PathEvent]] = {}
        for event in self.events:
            if not isinstance(event, Split | Bonus):
                by_day.setdefault(event.day, []).append(event)
        slope, previous, bars = self.slope, self.start, []
        for day in days:
            today = by_day.get(day, [])
            for event in today:
                if isinstance(event, Trend):
                    slope = event.slope
            gap = sum((e.amount for e in today if isinstance(e, Gap)), Decimal(0))
            lock = next((e for e in today if isinstance(e, CircuitLock)), None)
            if lock is not None:
                price = previous + lock.amount
                bar = Bar(day, price, price, price, price, self.volume)
            else:
                open_ = previous + gap
                close = open_ + slope
                low = min(open_, close) - self.half_range
                for event in today:
                    if isinstance(event, Dip):
                        low = min(low, close - event.depth)
                high = max(open_, close) + self.half_range
                bar = Bar(day, open_, high, low, close, self.volume)
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
            "faceVal": "10",
            "subject": a.subject,
        }
        for s in stocks
        for a in s.actions
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
