"""The cost model: what a Fill costs beyond its price, itemised (ADR 0003, risk-controls).

Code only, the same in every Engine Mode. The rates are a dated schedule
checked into the repository (``cost_rates.csv``, at the location
``costs.schedule`` in ``tradeline.toml`` configures), each row citing where
the rate is published, as the tick table is::

    in_force_from,charge,rate,citation
    2023-04-01,stt,0.001,Finance Act 2013; zerodha.com/charges
    2024-10-01,exchange,0.0000297,Zerodha bulletin 391488

A charge's rate is in force from its date until the charge's next row. The
first date must give every charge, and a trade before it is refused, so no
Fill is priced on a rate nobody checked.

A Fill is one scrip on one side on one day, one line of a contract note.
Brokerage is zero. Its charges are:

- STT: the value times ``stt``, buy and sell, rounded to the rupee;
- stamp duty: the value times ``stamp_duty`` on a buy, rounded to the rupee;
- the exchange's transaction charge and SEBI's turnover fee: the value times
  ``exchange`` and ``sebi``, to the paisa;
- GST: ``gst`` times the exchange charge and SEBI fee, to the paisa;
- on a sell, the DP charge (``dp_charge`` rupees per scrip per sell day, one
  Fill since a Position is the one holding in its Instrument) and GST on it;
- Slippage, the modelled cost beyond the fill price (GLOSSARY): the value
  times a safety margin plus an impact coefficient c times the Order's share
  of the Instrument's 20-day average daily traded value, to the paisa.

Rounding is half up, as Zerodha's brokerage calculator rounds. A backtest
prices every Fill on the schedule as of one stated date (``rates_as_of``: the
Protocol's freeze, #81), since the question is whether the Strategy clears the
costs it will pay from now on; a contract note, or a paper Fill, is priced on
the rates in force on its own day.

Real contract notes the Operator supplies are kept in ``contract_notes.csv``
(``load_contract_notes``), each a test against the rates on its trade date.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Literal

from tradeline.reviewed_csv import FirstLines, Line, parse_date, read_lines, require_header

Side = Literal["buy", "sell"]
CHARGES = ("stt", "stamp_duty", "exchange", "sebi", "gst", "dp_charge")
COLUMNS = ("in_force_from", "charge", "rate", "citation")
NOTE_COLUMNS = (
    "trade_date",
    "side",
    "price",
    "shares",
    "stt",
    "stamp_duty",
    "exchange_charge",
    "sebi_fee",
    "gst",
    "source",
)
_RUPEE = Decimal("1")
_PAISA = Decimal("0.01")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class Rates:
    """The rates in force on one day."""

    stt: Decimal
    stamp_duty: Decimal
    exchange: Decimal
    sebi: Decimal
    gst: Decimal
    # Rupees per scrip per sell day, before GST.
    dp_charge: Decimal


@dataclass(frozen=True)
class Rate:
    in_force_from: date
    charge: str
    rate: Decimal
    citation: str


@dataclass(frozen=True)
class CostSchedule:
    rates_by_date: tuple[Rate, ...]

    @property
    def first(self) -> date:
        return min(r.in_force_from for r in self.rates_by_date)

    def rates(self, day: date) -> Rates:
        """The rate of each charge in force on ``day``."""
        if day < self.first:
            raise ValueError(
                f"{day} is before the cost schedule's first date {self.first};"
                " add the rates in force then, with citations, to price it"
            )
        latest: dict[str, Rate] = {}
        for rate in self.rates_by_date:
            if rate.in_force_from <= day:
                found = latest.get(rate.charge)
                if found is None or found.in_force_from < rate.in_force_from:
                    latest[rate.charge] = rate
        return Rates(**{charge: latest[charge].rate for charge in CHARGES})


@dataclass(frozen=True)
class Slippage:
    """A safety margin on the Order's value per side, plus impact c x (Order value / 20-day
    average daily traded value) (ADR 0003)."""

    margin: Decimal = Decimal("0.0005")
    impact: Decimal = Decimal("0.1")


@dataclass(frozen=True)
class Charges:
    """One Fill's costs, itemised, in rupees."""

    stt: Decimal
    stamp_duty: Decimal
    exchange_charge: Decimal
    sebi_fee: Decimal
    gst: Decimal
    dp_charge: Decimal
    dp_gst: Decimal
    slippage: Decimal

    @property
    def statutory(self) -> Decimal:
        """Everything a contract note and the DP charge bill: all but Slippage."""
        return self.total - self.slippage

    @property
    def total(self) -> Decimal:
        return sum((getattr(self, f.name) for f in fields(self)), _ZERO)

    def items(self) -> dict[str, Decimal]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class CostModel:
    """The schedule and the Slippage parameters; ``rates_as_of`` fixes the schedule's date
    for every Fill (a backtest's), None prices each Fill on its own day's rates."""

    schedule: CostSchedule
    slippage: Slippage
    rates_as_of: date | None = None

    def charges(
        self,
        side: str,
        *,
        day: date,
        price: Decimal,
        shares: int,
        average_traded_value: Decimal,
    ) -> Charges:
        """What a Fill of ``shares`` at ``price`` on ``day`` costs beyond its price, for an
        Instrument trading ``average_traded_value`` rupees a day."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side {side!r} is not buy or sell")
        rates = self.schedule.rates(self.rates_as_of or day)
        value = price * shares
        exchange = paisa(value * rates.exchange)
        sebi = paisa(value * rates.sebi)
        selling = side == "sell"
        dp = rates.dp_charge if selling else _ZERO
        share = value / average_traded_value if average_traded_value > 0 else Decimal(1)
        return Charges(
            stt=_rupee(value * rates.stt),
            stamp_duty=_ZERO if selling else _rupee(value * rates.stamp_duty),
            exchange_charge=exchange,
            sebi_fee=sebi,
            gst=paisa((exchange + sebi) * rates.gst),
            dp_charge=dp,
            dp_gst=paisa(dp * rates.gst),
            slippage=paisa(value * (self.slippage.margin + self.slippage.impact * share)),
        )


def _rupee(value: Decimal) -> Decimal:
    return value.quantize(_RUPEE, rounding=ROUND_HALF_UP)


def paisa(value: Decimal | Fraction) -> Decimal:
    """``value`` to the paisa, half up."""
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    return value.quantize(_PAISA, rounding=ROUND_HALF_UP)


def load_cost_schedule(path: Path) -> CostSchedule:
    """The rate schedule at ``path``; any bad row refuses the file."""
    lines = read_lines(path, "cost schedule")
    require_header(path, lines[0] if lines else None, COLUMNS, "first")
    seen: FirstLines[tuple[date, str]] = FirstLines()
    rates = []
    for line in lines[1:]:
        rate = _rate(line)
        seen.add(
            (rate.in_force_from, rate.charge), line, f"{rate.charge} on {rate.in_force_from} again"
        )
        rates.append(rate)
    if not rates:
        raise ValueError(f"{path}: no rates")
    first = min(r.in_force_from for r in rates)
    for charge in CHARGES:
        if not any(r.charge == charge and r.in_force_from == first for r in rates):
            raise ValueError(f"{path}: the first date {first} has no rate for {charge}")
    return CostSchedule(tuple(rates))


def _rate(line: Line) -> Rate:
    day_text, charge, rate_text, citation = line.columns(COLUMNS)
    day = parse_date(line.where, "in_force_from", day_text)
    where = f"{line.where}: {day}"
    if charge not in CHARGES:
        raise ValueError(f"{where}: unknown charge {charge!r}; one of {', '.join(CHARGES)}")
    rate = _amount(where, "rate", rate_text)
    if not citation:
        raise ValueError(f"{where}: no citation; cite where the rate is published")
    return Rate(day, charge, rate, citation)


def _amount(where: str, column: str, text: str) -> Decimal:
    try:
        amount = Decimal(text)
    except InvalidOperation:
        amount = Decimal(-1)
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{where}: {column} {text!r} is not a non-negative number")
    return amount


@dataclass(frozen=True)
class ContractNote:
    """One trade as a real contract note states it, and what it was charged."""

    trade_date: date
    side: Side
    price: Decimal
    shares: int
    stt: Decimal
    stamp_duty: Decimal
    exchange_charge: Decimal
    sebi_fee: Decimal
    gst: Decimal
    source: str


def load_contract_notes(path: Path) -> tuple[ContractNote, ...]:
    """The real contract notes at ``path``, none when the Operator has supplied none."""
    lines = read_lines(path, "contract notes")
    require_header(path, lines[0] if lines else None, NOTE_COLUMNS, "first")
    return tuple(_note(line) for line in lines[1:])


def _note(line: Line) -> ContractNote:
    day_text, side, price, shares, stt, stamp, exchange, sebi, gst, source = line.columns(
        NOTE_COLUMNS
    )
    day = parse_date(line.where, "trade_date", day_text)
    where = f"{line.where}: {day}"
    if side not in ("buy", "sell"):
        raise ValueError(f"{where}: side {side!r} is not buy or sell")
    if not shares.isdigit() or int(shares) == 0:
        raise ValueError(f"{where}: shares {shares!r} is not a positive whole number")
    if not source:
        raise ValueError(f"{where}: no source; name the contract note")
    return ContractNote(
        day,
        "buy" if side == "buy" else "sell",
        _amount(where, "price", price),
        int(shares),
        _amount(where, "stt", stt),
        _amount(where, "stamp_duty", stamp),
        _amount(where, "exchange_charge", exchange),
        _amount(where, "sebi_fee", sebi),
        _amount(where, "gst", gst),
        source,
    )
