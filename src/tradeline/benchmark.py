"""The Benchmark: which fund it is on a day, and its NAV as of a day (ADR 0015 amendment).

The Benchmark is the direct plan, growth option, of a real NIFTY 500 index
fund, valued at its AMFI NAV. Which scheme it is comes only from the reviewed,
checked-in file at ``benchmark.funds`` in ``tradeline.toml``
(``benchmark_funds.csv``)::

    in_force_from,scheme_code,event,citation
    2019-09-11,147625,chosen,ADR 0015 amendment of 2026-10-25; ...

The first row's event is ``chosen``; a later row changes the scheme from its
date and must record one of ``FUND_CHANGE_EVENTS``, the fund's merger, closure
or change of index, with a citation. Any other row refuses the file.

``ingest benchmark`` (``tradeline.ingest.benchmark``) stores AMFI's scheme
master lines and NAV history. Reading the Benchmark checks the stored master
line names its scheme with both ``"Direct"`` and ``"Growth"``
(``NotDirectGrowth``), as the ingest does. ``nav_as_of`` never returns a NAV
dated after the day asked about.

An account's Benchmark (``benchmark_liquidation``) is bought on the account's
first day for its starting Equity: the fund house deducts stamp duty and allots
units for the rest at that day's NAV, unrounded. On each day it is valued at
the NAV as of the day and liquidated: STT and any exit load are paid on the
redemption, and the gain over the amount paid is taxed once, as one lot
(``tradeline.tax``), short-term or long-term by the same holding rule as a share.
A Protocol freezes the funds, the factor fund and the fund-unit rates
(``BenchmarkTerms``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg

from tradeline.costs import FundCharges, FundRates, paisa
from tradeline.reviewed_csv import Line, parse_date, read_lines, require_header
from tradeline.tax import Liquidation, Sale, TaxTerms, is_short_term, liquidation_value
from tradeline.trading_calendar import TradingCalendar

COLUMNS = ("in_force_from", "scheme_code", "event", "citation")
FIRST_EVENT = "chosen"
FUND_CHANGE_EVENTS = ("merger", "closure", "index_change")
# Words AMFI's master must use in the line naming a Benchmark scheme.
DIRECT_GROWTH = ("Direct", "Growth")


class NotDirectGrowth(ValueError):
    """AMFI's scheme master does not name a Benchmark scheme as a direct-plan growth option."""


@dataclass(frozen=True)
class FundChange:
    in_force_from: date
    scheme_code: int
    event: str
    citation: str


@dataclass(frozen=True)
class BenchmarkFunds:
    """The Benchmark's scheme from each recorded date, in date order."""

    changes: tuple[FundChange, ...]

    @property
    def first(self) -> date:
        return self.changes[0].in_force_from

    @property
    def schemes(self) -> tuple[int, ...]:
        """Every scheme the Benchmark has been, in order."""
        return tuple(c.scheme_code for c in self.changes)

    def scheme_on(self, day: date) -> int:
        return self.change_on(day).scheme_code

    def change_on(self, day: date) -> FundChange:
        """The row in force on ``day``."""
        if day < self.first:
            raise ValueError(f"{day} is before the Benchmark's first date {self.first}")
        return [c for c in self.changes if c.in_force_from <= day][-1]

    def span(self, scheme_code: int) -> tuple[date, date | None]:
        """The first day ``scheme_code`` is the Benchmark, and the last (None while it is)."""
        for index, change in enumerate(self.changes):
            if change.scheme_code == scheme_code:
                after = self.changes[index + 1 :]
                last = None if not after else after[0].in_force_from - timedelta(days=1)
                return change.in_force_from, last
        raise ValueError(f"scheme {scheme_code} was never the Benchmark")


@dataclass(frozen=True)
class BenchmarkTerms:
    """What a Protocol freezes of the Benchmark: its funds from each recorded date, the
    factor fund reported beside it, and the fund-unit rates as of the freeze."""

    funds: BenchmarkFunds
    factor_fund: int
    rates: FundRates


@dataclass(frozen=True)
class BenchmarkLiquidation:
    """An account's Benchmark liquidated on a day: its units, their value at the day's NAV,
    the charges on its purchase and its redemption, whether its gain is short-term, and
    its Liquidation Value after the tax on that gain."""

    units: Decimal
    value: Decimal
    purchase: FundCharges
    sale: FundCharges
    short_term: bool
    liquidation: Liquidation


def benchmark_liquidation(
    *,
    bought_on: date,
    bought_nav: Decimal,
    day: date,
    nav: Decimal,
    invested: Decimal,
    rates: FundRates,
    terms: TaxTerms,
    calendar: TradingCalendar,
) -> BenchmarkLiquidation:
    """The Benchmark bought for ``invested`` on ``bought_on`` at ``bought_nav``, redeemed on
    ``day`` at ``nav``: the gain over ``invested`` is taxed once, on the redemption."""
    found = benchmark_redemption(
        bought_on=bought_on, bought_nav=bought_nav, day=day, nav=nav, invested=invested, rates=rates
    )
    return BenchmarkLiquidation(
        units=found.units,
        value=found.value,
        purchase=found.purchase,
        sale=found.sale,
        short_term=is_short_term(bought_on, day),
        liquidation=liquidation_value(
            day, cash=Decimal(0), holdings=[found.lot], sales=[], terms=terms, calendar=calendar
        ),
    )


@dataclass(frozen=True)
class BenchmarkRedemption:
    """The Benchmark's units, their value at a day's NAV, its purchase's and redemption's
    charges, and the lot the tax ledger takes: the amount paid against the redemption's
    value less its charges."""

    units: Decimal
    value: Decimal
    purchase: FundCharges
    sale: FundCharges
    lot: Sale


def benchmark_redemption(
    *,
    bought_on: date,
    bought_nav: Decimal,
    day: date,
    nav: Decimal,
    invested: Decimal,
    rates: FundRates,
) -> BenchmarkRedemption:
    """The Benchmark bought for ``invested`` on ``bought_on`` at ``bought_nav`` and redeemed on
    ``day`` at ``nav``, before tax."""
    purchase = rates.charges("buy", value=invested, bought_on=bought_on, day=bought_on)
    units = (invested - purchase.stamp_duty) / bought_nav
    value = paisa(units * nav)
    sale = rates.charges("sell", value=value, bought_on=bought_on, day=day)
    lot = Sale(bought=bought_on, sold=day, basis=invested, proceeds=value - sale.total)
    return BenchmarkRedemption(units, value, purchase, sale, lot)


def load_benchmark_funds(path: Path) -> BenchmarkFunds:
    """The Benchmark's funds file at ``path``; any bad row refuses the file."""
    lines = read_lines(path, "Benchmark funds")
    require_header(path, lines[0] if lines else None, COLUMNS, "first")
    changes: list[FundChange] = []
    for line in lines[1:]:
        change = _change(line)
        if not changes:
            if change.event != FIRST_EVENT:
                raise ValueError(f"{line.where}: the first row's event must be {FIRST_EVENT!r}")
        else:
            before = changes[-1]
            if change.event not in FUND_CHANGE_EVENTS:
                raise ValueError(
                    f"{line.where}: {change.event!r} is not a fund-change event;"
                    f" one of {', '.join(FUND_CHANGE_EVENTS)}"
                )
            if change.in_force_from <= before.in_force_from:
                raise ValueError(
                    f"{line.where}: {change.in_force_from} is not after {before.in_force_from}"
                )
            if change.scheme_code == before.scheme_code:
                raise ValueError(f"{line.where}: {change.scheme_code} is already the Benchmark")
        changes.append(change)
    if not changes:
        raise ValueError(f"{path}: no rows")
    return BenchmarkFunds(tuple(changes))


def _change(line: Line) -> FundChange:
    day_text, code, event, citation = line.columns(COLUMNS)
    day = parse_date(line.where, "in_force_from", day_text)
    if not code.isdigit():
        raise ValueError(f"{line.where}: {code!r} is not an AMFI scheme code")
    if not citation:
        raise ValueError(f"{line.where}: no citation; cite the notice of the event")
    return FundChange(day, int(code), event, citation)


@dataclass(frozen=True)
class Scheme:
    """A scheme's line in AMFI's master, as stored."""

    scheme_code: int
    scheme_name: str
    plan: str
    option: str
    isin_growth: str | None
    isin_reinvestment: str | None
    fetched_at: datetime

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.scheme_name, self.plan, self.option) if part)

    @property
    def is_direct_growth(self) -> bool:
        return all(word in self.full_name for word in DIRECT_GROWTH)


@dataclass(frozen=True)
class Nav:
    scheme_code: int
    nav_date: date
    nav: Decimal


def scheme(conn: psycopg.Connection, scheme_code: int, *, as_of: datetime | None = None) -> Scheme:
    """``scheme_code``'s latest stored master line, or the latest fetched by ``as_of``."""
    row = conn.execute(
        "SELECT scheme_code, scheme_name, plan, option, isin_growth, isin_reinvestment,"
        " fetched_at FROM amfi_schemes"
        " WHERE scheme_code = %s AND (%s::timestamptz IS NULL OR fetched_at <= %s)"
        " ORDER BY fetched_at DESC LIMIT 1",
        (scheme_code, as_of, as_of),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"AMFI scheme {scheme_code} has no stored master line; run `ingest benchmark`"
        )
    return Scheme(*row)


def benchmark_scheme(conn: psycopg.Connection, funds: BenchmarkFunds, day: date) -> Scheme:
    """The Benchmark's scheme on ``day``, refused unless AMFI names it Direct and Growth."""
    found = scheme(conn, funds.scheme_on(day))
    require_direct_growth(found.scheme_code, found.full_name)
    return found


def require_direct_growth(scheme_code: int, full_name: str) -> None:
    missing = [word for word in DIRECT_GROWTH if word not in full_name]
    if missing:
        raise NotDirectGrowth(
            f"AMFI's scheme master names Benchmark scheme {scheme_code} {full_name!r},"
            f" without {' or '.join(repr(w) for w in missing)}: it is not the direct plan,"
            " growth option"
        )


def nav_as_of(conn: psycopg.Connection, scheme_code: int, day: date) -> Nav | None:
    """``scheme_code``'s latest NAV dated on or before ``day``; None before its first."""
    row = conn.execute(
        "SELECT scheme_code, nav_date, nav FROM amfi_navs"
        " WHERE scheme_code = %s AND nav_date <= %s"
        " ORDER BY nav_date DESC, fetched_at DESC LIMIT 1",
        (scheme_code, day),
    ).fetchone()
    return None if row is None else Nav(*row)


def navs(conn: psycopg.Connection, scheme_code: int, *, start: date, end: date) -> list[Nav]:
    """``scheme_code``'s NAV on each date AMFI published one from ``start`` to ``end``."""
    rows = conn.execute(
        "SELECT DISTINCT ON (nav_date) scheme_code, nav_date, nav FROM amfi_navs"
        " WHERE scheme_code = %s AND nav_date BETWEEN %s AND %s"
        " ORDER BY nav_date, fetched_at DESC",
        (scheme_code, start, end),
    ).fetchall()
    return [Nav(*row) for row in rows]


def benchmark_nav(conn: psycopg.Connection, funds: BenchmarkFunds, day: date) -> Nav | None:
    """The Benchmark's NAV as of ``day``: its scheme on ``day``, checked, at its latest NAV
    dated on or before ``day``."""
    return nav_as_of(conn, benchmark_scheme(conn, funds, day).scheme_code, day)
