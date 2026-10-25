"""The tax ledger: what a portfolio account owes in tax, year by year (ADR 0015 amendment).

A pure function over an account's realised events: each sale of a lot, each
dividend and each Demerger Credit, with the dated tax schedule applied as of
one date (the Protocol's freeze), as costs are. It returns, for each Indian
financial year (April 1 to March 31), the net tax and the day it is debited
from cash, the first trading day after March 31, and the losses carried out
of the year. The Benchmark goes through the same function as one lot.

The rules (``docs/risk-controls.md``, "Tax (portfolio families)"):

- a lot sold no more than twelve months after it was bought is a short-term
  gain or loss, otherwise long-term (``is_short_term``);
- short-term gains at the schedule's ``short_term`` rate, long-term at its
  ``long_term`` rate, each plus its ``cess``; surcharge is not modelled and the
  Rs 1.25 lakh long-term exemption is not applied;
- dividends at the configured ``dividend_rate``, which already includes cess
  (default 31.2 percent, the 30 percent slab plus cess);
- a Demerger Credit is a sale of the child's shares against the child's share
  of the parent's basis, held since the parent was bought;
- each year's net tax is rounded to the paisa, half up.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from tradeline.costs import paisa
from tradeline.trading_calendar import TradingCalendar

TaxItem = Literal["short_term", "long_term", "cess"]
ITEMS: tuple[TaxItem, ...] = ("short_term", "long_term", "cess")
DEFAULT_DIVIDEND_RATE = Decimal("0.312")
# A loss is carried for the eight financial years after its own (s. 74(3)).
CARRY_YEARS = 8
_ZERO = Decimal("0")


@dataclass(frozen=True)
class TaxRate:
    """A rate in force from ``in_force_from`` until the item's next row, citing its source."""

    in_force_from: date
    item: TaxItem
    rate: Decimal
    citation: str


@dataclass(frozen=True)
class TaxRates:
    """The rates in force on one day, before cess."""

    short_term: Decimal
    long_term: Decimal
    cess: Decimal


@dataclass(frozen=True)
class TaxSchedule:
    rows: tuple[TaxRate, ...]

    def rates(self, day: date) -> TaxRates:
        """The rate of each item in force on ``day``."""
        latest: dict[str, TaxRate] = {}
        for row in self.rows:
            if row.in_force_from <= day:
                found = latest.get(row.item)
                if found is None or found.in_force_from < row.in_force_from:
                    latest[row.item] = row
        missing = [item for item in ITEMS if item not in latest]
        if missing:
            raise ValueError(f"the tax schedule gives no {', '.join(missing)} rate on {day}")
        return TaxRates(**{item: latest[item].rate for item in ITEMS})


@dataclass(frozen=True)
class TaxTerms:
    """The schedule, the date it is applied as of (the Protocol's freeze) for every event,
    and the configured dividend rate, cess included."""

    schedule: TaxSchedule
    rates_as_of: date
    dividend_rate: Decimal = DEFAULT_DIVIDEND_RATE


@dataclass(frozen=True)
class Sale:
    """One lot bought on ``bought`` and sold on ``sold``: its basis and proceeds, after costs."""

    bought: date
    sold: date
    basis: Decimal
    proceeds: Decimal

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.basis


@dataclass(frozen=True)
class Dividend:
    """Cash credited on ``day``, an Ex-Date, for a dividend on a held Position."""

    day: date
    amount: Decimal


@dataclass(frozen=True)
class DemergerCredit:
    """Cash credited on ``day``, the child's first listing day, for the child's shares of a
    parent lot bought on ``parent_bought``; ``basis`` is the child's share of that lot's
    basis (zero if no split was announced)."""

    day: date
    parent_bought: date
    amount: Decimal
    basis: Decimal

    def sale(self) -> Sale:
        """The credit as a sale of the child's shares, held since the parent's were (the
        Income-tax Act, 1961, s. 2(42A), Explanation 1(i)(g))."""
        return Sale(
            bought=self.parent_bought, sold=self.day, basis=self.basis, proceeds=self.amount
        )


@dataclass(frozen=True)
class Losses:
    """Capital losses by kind, in rupees, each zero or positive."""

    short_term: Decimal
    long_term: Decimal

    @property
    def total(self) -> Decimal:
        return self.short_term + self.long_term


NO_LOSSES = Losses(_ZERO, _ZERO)


@dataclass(frozen=True)
class TaxYear:
    # The financial year's first calendar year: 2025 is April 2025 to March 2026.
    year: int
    # The year's net gains of each kind after every set-off, and its dividends.
    short_term_gain: Decimal
    long_term_gain: Decimal
    dividends: Decimal
    tax: Decimal
    debit_day: date
    # Every loss still usable after this year, of whichever year it arose in.
    carried_forward: Losses
    # The losses whose eighth year after their own this was, left unused: they lapse.
    expired: Losses


@dataclass(frozen=True)
class TaxLedger:
    years: tuple[TaxYear, ...]
    # At a liquidation, the losses left unused: they are dropped, and the report says how
    # much. Nothing without one.
    losses_dropped: Losses = NO_LOSSES


@dataclass(frozen=True)
class Liquidation:
    """An account's Liquidation Value on a day, the tax it leaves owing, and the ledger."""

    value: Decimal
    tax_due: Decimal
    losses_dropped: Losses
    ledger: TaxLedger


@dataclass
class _Loss:
    # The financial year it arose in, its kind, and the part still unused.
    year: int
    short_term: bool
    left: Decimal


def anniversary(bought: date) -> date:
    """The day twelve months after ``bought``; February 29 falls on February 28."""
    try:
        return bought.replace(year=bought.year + 1)
    except ValueError:
        return bought.replace(year=bought.year + 1, day=28)


def is_short_term(bought: date, sold: date) -> bool:
    """Held "not more than twelve months": the Income-tax Act, 1961, s. 2(42A), and from
    2026-04-01 the Income-tax Act, 2025, s. 2(101)(b). Sold on the anniversary is
    exactly twelve months, so short-term."""
    return sold <= anniversary(bought)


def financial_year(day: date) -> int:
    """The first calendar year of the Indian financial year holding ``day``."""
    return day.year if day.month >= 4 else day.year - 1


def debit_day(year: int, calendar: TradingCalendar) -> date:
    """The first trading day after financial year ``year``'s March 31."""
    april = date(year + 1, 4, 1)
    return next(calendar.trading_days(april, calendar.last))


def tax_ledger(
    sales: Iterable[Sale],
    dividends: Iterable[Dividend] = (),
    credits: Iterable[DemergerCredit] = (),
    *,
    terms: TaxTerms,
    calendar: TradingCalendar,
    liquidated_on: date | None = None,
) -> TaxLedger:
    """Each financial year's net tax on ``sales``, ``dividends`` and Demerger ``credits``
    and the day it is debited, for every year from the first event's to the last's.

    With ``liquidated_on`` the account ends that day: no event may follow it, the
    ledger runs to its financial year, that final partial year is settled on the day
    itself, and the losses still unused are dropped and returned. A year still open
    without one carries its tax so far, due on its April debit day.
    """
    rates = terms.schedule.rates(terms.rates_as_of)
    realised = [*sales, *(c.sale() for c in credits)]
    dividends = list(dividends)
    if liquidated_on is not None:
        for day in sorted({s.sold for s in realised} | {d.day for d in dividends}):
            if day > liquidated_on:
                raise ValueError(f"an event on {day} is after the liquidation on {liquidated_on}")
    gains: dict[tuple[int, bool], list[Decimal]] = {}
    for s in realised:
        gains.setdefault((financial_year(s.sold), is_short_term(s.bought, s.sold)), []).append(
            s.gain
        )
    income: dict[int, Decimal] = {}
    for dividend in dividends:
        year = financial_year(dividend.day)
        income[year] = income.get(year, _ZERO) + dividend.amount
    event_years = {y for y, _ in gains} | set(income)
    if liquidated_on is not None:
        event_years.add(financial_year(liquidated_on))
    if not event_years:
        return TaxLedger(())
    first, last = min(event_years), max(event_years)
    pool: list[_Loss] = []
    years = []
    for year in range(first, last + 1):
        short, long = _set_off(
            gains.get((year, True), []), gains.get((year, False), []), year, pool
        )
        paid = income.get(year, _ZERO)
        tax = (short * rates.short_term + long * rates.long_term) * (
            1 + rates.cess
        ) + paid * terms.dividend_rate
        final = liquidated_on is not None and year == last
        expired = NO_LOSSES if final else _expire(year, pool)
        years.append(
            TaxYear(
                year,
                short_term_gain=short,
                long_term_gain=long,
                dividends=paid,
                tax=paisa(tax),
                debit_day=liquidated_on if final and liquidated_on else debit_day(year, calendar),
                carried_forward=_losses(pool),
                expired=expired,
            )
        )
    dropped = years[-1].carried_forward if liquidated_on is not None else NO_LOSSES
    return TaxLedger(tuple(years), dropped)


def liquidation_value(
    day: date,
    *,
    cash: Decimal,
    holdings: Iterable[Sale],
    sales: Iterable[Sale],
    dividends: Iterable[Dividend] = (),
    credits: Iterable[DemergerCredit] = (),
    terms: TaxTerms,
    calendar: TradingCalendar,
) -> Liquidation:
    """What the account would hold in cash if it sold everything on ``day`` and paid every
    tax owed to then (GLOSSARY, Liquidation Value).

    ``cash`` is the account's cash on ``day`` before any tax debit falling on it (the
    debits of earlier days already paid); ``holdings`` are its open lots, each a sale
    on ``day`` at its proceeds after selling costs; ``sales``, ``dividends`` and
    ``credits`` are everything realised before. The Benchmark is one holding.
    """
    held = list(holdings)
    for lot in held:
        if lot.sold != day:
            raise ValueError(
                f"a holding bought {lot.bought} is sold on {lot.sold}, not on the liquidation"
                f" day {day}"
            )
    ledger = tax_ledger(
        [*sales, *held],
        dividends,
        credits,
        terms=terms,
        calendar=calendar,
        liquidated_on=day,
    )
    due = sum((y.tax for y in ledger.years if y.debit_day >= day), _ZERO)
    proceeds = sum((lot.proceeds for lot in held), _ZERO)
    return Liquidation(paisa(cash + proceeds - due), paisa(due), ledger.losses_dropped, ledger)


def _set_off(
    short_results: list[Decimal], long_results: list[Decimal], year: int, pool: list[_Loss]
) -> tuple[Decimal, Decimal]:
    """The year's taxable short-term and long-term gains, after setting off its own losses
    and then losses carried into it; the year's unused losses join ``pool``.

    Within the year (the Income-tax Act, 1961, s. 70): a short-term loss against
    short-term gains first, which are taxed at the higher rate, then long-term; a
    long-term loss against long-term only. Carried losses (s. 74), oldest first:
    long-term losses against long-term gains, then short-term losses against what is
    left of short-term gains and then long-term, keeping the loss usable against
    either kind for last.
    """
    short = sum(short_results, _ZERO)
    long = sum(long_results, _ZERO)
    if short < 0 and long > 0:
        used = min(-short, long)
        short, long = short + used, long - used
    carried = sorted(pool, key=lambda loss: loss.year)
    for loss in (c for c in carried if not c.short_term):
        long -= _use(loss, long)
    for loss in (c for c in carried if c.short_term):
        short -= _use(loss, short)
        long -= _use(loss, long)
    pool[:] = [loss for loss in pool if loss.left > 0]
    pool.extend(_Loss(year, kind, -net) for kind, net in ((True, short), (False, long)) if net < 0)
    return max(short, _ZERO), max(long, _ZERO)


def _use(loss: _Loss, gain: Decimal) -> Decimal:
    """Set off as much of ``loss`` as ``gain`` absorbs; the amount used."""
    used = min(loss.left, max(gain, _ZERO))
    loss.left -= used
    return used


def _expire(year: int, pool: list[_Loss]) -> Losses:
    """Remove the losses whose eighth year after their own is ``year``; what lapsed."""
    lapsing = [loss for loss in pool if loss.year + CARRY_YEARS <= year]
    pool[:] = [loss for loss in pool if loss.year + CARRY_YEARS > year]
    return _losses(lapsing)


def _losses(pool: list[_Loss]) -> Losses:
    return Losses(
        sum((loss.left for loss in pool if loss.short_term), _ZERO),
        sum((loss.left for loss in pool if not loss.short_term), _ZERO),
    )
