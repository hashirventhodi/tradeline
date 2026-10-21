"""Our own corporate-action adjustment (ADR 0006): what adjusts, by how much, and the arithmetic.

Two pure functions. ``interpret`` reads one NSE corporate-action subject
(``"Bonus 1:1"``, ``"Face Value Split (Sub-Division) - From Rs 10/- Per Share
To Re 1/- Per Share"``) into a kind and, for splits and bonuses, the price
factor. ``adjust`` applies a symbol's actions to its raw bars as of a date.

Splits (either direction) and bonuses are adjusted: every bar before the
ex-date has its prices multiplied by the factor and its volume divided by it,
compounding across actions. Rights issues and dividends are recorded and
flagged on the bars they precede but left unadjusted, since adjusting them
needs a price (the ex-date close) and a convention, and the broker's chart
does not adjust them either. Meetings, buybacks, and the rest are recorded only.

Factors are exact fractions (a 1:5 bonus is 5/6), and an adjusted price is
rounded to the exchange tick of 5 paisa with ties to the even tick. Checked on
2026-10-21 against Kite's displayed RELIANCE bars across its 1:1 bonus of
2024-10-28: 43 days, 172 prices, 55 of them ties, all equal. Rounding to the
paisa, or ties away from zero, does not reproduce them. NSE's 1 paisa tick
for prices under Rs 250 (since June 2024) is not modelled; no sample yet
shows what the broker does there.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Literal

Kind = Literal["split", "bonus", "rights", "dividend", "other"]
KINDS: tuple[Kind, ...] = ("split", "bonus", "rights", "dividend", "other")
ADJUSTED_KINDS: frozenset[str] = frozenset({"split", "bonus"})
FLAGGED_KINDS: frozenset[str] = frozenset({"rights", "dividend"})

TICK = Decimal("0.05")
_TICK = Fraction(TICK)
_PAISA = Decimal("0.01")


@dataclass(frozen=True)
class CorporateAction:
    """One NSE corporate action with the factor we derived from its subject.

    ``factor`` multiplies the prices of every bar before ``ex_date``; it is
    below one for a split or bonus, above one for a consolidation, and None
    for everything that is not adjusted.
    """

    symbol: str
    series: str
    ex_date: date
    subject: str
    kind: Kind
    factor: Fraction | None
    isin: str | None = None
    record_date: date | None = None
    face_value: Decimal | None = None


@dataclass(frozen=True)
class RawBar:
    symbol: str
    series: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int


@dataclass(frozen=True)
class AdjustedBar:
    symbol: str
    series: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int
    # The cumulative price factor applied to this bar; 1 when nothing was.
    factor: Fraction
    # Later actions that were recorded but not applied (rights, dividends), oldest first.
    unadjusted: tuple[CorporateAction, ...]


def adjust(
    bars: Iterable[RawBar], actions: Iterable[CorporateAction], *, as_of: date
) -> list[AdjustedBar]:
    """The bars with every action dated on or before ``as_of`` applied, in the order given.

    An action adjusts (or flags) exactly the bars dated before its ex-date, so
    an action dated on a holiday adjusts everything before it and nothing
    after. NSE can list one action more than once (under each series the
    symbol trades in, or again with a revised subject); it is applied once.
    """
    known = _distinct(a for a in actions if a.ex_date <= as_of)
    # Suffix products: factors[i] is the product of the factors of known[i:],
    # the actions after a bar whose date sorts just before known[i].
    factors = [Fraction(1)] * (len(known) + 1)
    flagged: list[tuple[CorporateAction, ...]] = [()] * (len(known) + 1)
    for i in range(len(known) - 1, -1, -1):
        a = known[i]
        factors[i], flagged[i] = factors[i + 1], flagged[i + 1]
        if a.kind in ADJUSTED_KINDS:
            if a.factor is None:
                raise ValueError(f"{a.kind} action for {a.symbol} on {a.ex_date} has no factor")
            factors[i] = factors[i + 1] * a.factor
        elif a.kind in FLAGGED_KINDS:
            flagged[i] = (a, *flagged[i + 1])
    ex_dates = [a.ex_date for a in known]
    adjusted = []
    for bar in bars:
        i = bisect_right(ex_dates, bar.trade_date)
        factor = factors[i]
        adjusted.append(
            AdjustedBar(
                symbol=bar.symbol,
                series=bar.series,
                trade_date=bar.trade_date,
                open=_price(bar.open, factor),
                high=_price(bar.high, factor),
                low=_price(bar.low, factor),
                close=_price(bar.close, factor),
                volume=_volume(bar.volume, factor),
                factor=factor,
                unadjusted=flagged[i],
            )
        )
    return adjusted


def _distinct(actions: Iterable[CorporateAction]) -> list[CorporateAction]:
    """One action per (ex-date, kind, factor), or per subject without a factor; by ex-date."""
    distinct: dict[tuple[object, ...], CorporateAction] = {}
    for a in sorted(actions, key=lambda a: a.ex_date):
        key = (a.ex_date, a.kind, a.factor if a.kind in ADJUSTED_KINDS else a.subject)
        distinct.setdefault(key, a)
    return list(distinct.values())


def _price(price: Decimal | None, factor: Fraction) -> Decimal | None:
    if price is None or factor == 1:
        return price
    ticks = round(Fraction(price) * factor / _TICK)  # exact, and ties go to the even tick
    return (ticks * TICK).quantize(_PAISA)


def _volume(volume: int, factor: Fraction) -> int:
    return volume if factor == 1 else round(Fraction(volume) / factor)


# --- Reading NSE's subjects -------------------------------------------------

# "Bonus 1:1", "Bonus- 1:2", "Bonus 1: 25". Not "Bonus Ncrps 1:1" nor "Bonus - 1 Debenture".
_BONUS = re.compile(r"\bbonus\s*-?\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)
# "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
# "Face Value Split From Rs 2 To Re 1", "Consolidation Of Equity Shares From Re 1 ... To Rs 10",
# and, for a face value split only, "Face Value Split Rs 10 To Rs 1" without the "From"
# and the 2016 to 2018 short form "Fv Splt Frm Rs 10 To Re 1".
_SPLIT = re.compile(
    r"(?:(?:face\s*value\s*split|sub-?division|consolidation)[^0-9]*?\bfrom"
    r"|face\s*value\s*split|\bfv\s*splt\s*frm)"
    r"\s*r[se]\.?\s*(\d+(?:\.\d+)?)[^0-9]*?\bto\s*r[se]\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RIGHTS = re.compile(r"\brights?\b", re.IGNORECASE)
_DIVIDEND = re.compile(r"\bdiv(?:idend)?\b|\bdistribution\b", re.IGNORECASE)
_ADJUSTING_WORDS = re.compile(r"bonus|spli?t|sub-?division|consolidation", re.IGNORECASE)
# A bonus of debentures or preference shares, or a split of something else, is not an equity action.
_NOT_EQUITY = re.compile(r"ncrps|debenture|preference|warrant", re.IGNORECASE)


def interpret(subject: str) -> tuple[Kind, Fraction | None]:
    """The kind of action an NSE subject describes and, when it is adjusted, its factor.

    A subject can name several actions ("Bonus 1:1/Face Value Split ..."):
    the adjusting ones compound into one factor, and the kind is the most
    consequential one named, in the order split, bonus, rights, dividend.
    """
    factor = Fraction(1)
    kind: Kind | None = None
    if not _NOT_EQUITY.search(subject):
        split = _SPLIT.search(subject)
        if split is not None:
            factor *= Fraction(split.group(2)) / Fraction(split.group(1))
            kind = "split"
        bonus = _BONUS.search(subject)
        if bonus is not None:
            extra, held = int(bonus.group(1)), int(bonus.group(2))
            factor *= Fraction(held, extra + held)
            kind = kind or "bonus"
    if kind is not None:
        return kind, factor
    if _RIGHTS.search(subject):
        return "rights", None
    if _DIVIDEND.search(subject):
        return "dividend", None
    return "other", None


def looks_adjusting(subject: str) -> bool:
    """Whether a subject mentions a split, bonus, or consolidation in equity shares.

    Used to notice a subject that ``interpret`` could not give a factor to,
    which would otherwise be stored as unadjusted without anyone knowing.
    """
    return bool(_ADJUSTING_WORDS.search(subject)) and not _NOT_EQUITY.search(subject)
