"""Dividends read from NSE's Corporate Action subjects, the cash a holder is credited (ADR 0020).

ADR 0006 adjusts no dividend; a dividend is cash. A Position held at the close
before a dividend's Ex-Date is credited, on the Ex-Date, its shares times the
amount per share the subject states (``tradeline.engine``, when the Protocol
turns crediting on).

``read_dividend`` reads one subject. A subject names a dividend when it says
so in any of NSE's spellings (``tradeline.adjust``'s reading of a dividend),
whatever kind the action was stored as: "Bonus 1:1 /Dividend- Rs 29 Per Share"
is a bonus in the Adjusted Series and a Rs 29 dividend in cash. It is read in
parts split at each "/" that does not end an amount ("Rs 4/-"), and each part
that names a dividend gives one amount for each dividend it names:

- per share: "Rs 5", "Re 0.50", "Rs. 3.30/-", or a bare "1.40 Per Share";
- a percent of face value: "25%", "25 per cent", "25 percent", times the face
  value NSE stored with the action;
- several in one subject, each credited: "Interim Dividend - Rs 10 Per Share
  Special Dividend - Rs 66 Per Share" is Rs 76.

Anything else is unparsed, with the reason, and is credited nothing: a subject
with no amount, a part whose count of amounts is not its count of dividends, an
amount in a part naming no dividend (a rights premium, a split's face values),
an amount of zero, a percent with no stored face value, a total stated as
including another ("Including Special Dividend"), or a unit's distribution (a
fund's or trust's, made of interest and return of capital). An unparsed subject
is never dropped: ``dividend_check`` lists it with its reason and counts it, the
input to the pre-Holdout check (ADR 0007, 2026-10-25 amendment), which passes only
when each is also in the reviewed list of unparsed subjects
(``load_listed_dividends``), the Operator's reason why crediting it nothing is right.

NSE can list one action under each series or Symbol; ``dividends`` keeps one per
Ex-Date and subject. Two subjects on one Ex-Date are two dividends, as NSE files a
final and an interim dividend (LT 2023-08-02: Rs 24 and a special Rs 6).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg

from tradeline import queries
from tradeline.adjust import CorporateAction
from tradeline.reviewed_csv import FirstLines, parse_date, read_lines, require_header

# NSE's spellings of a dividend, as ``tradeline.adjust`` reads them, without its
# "distribution" (a unit's, below). "Spl Div", "Int Div" and "Meetingdividend" included.
_DIVIDEND = re.compile(r"\bdiv\b|div(?:idend|dend|ided|idned|ivend)", re.IGNORECASE)
# A fund's or trust's payout, made of interest and return of capital, and what it is per.
_DISTRIBUTION = re.compile(r"\bdistri\w*", re.IGNORECASE)
_UNIT = re.compile(r"per\s*unit|\bdistri\w*", re.IGNORECASE)
_INCLUDING = re.compile(r"includ", re.IGNORECASE)
# One amount: rupees ("Rs 5", "Re. 0.50", "Rs-5"), a percent of face value, or a bare
# number followed by "Per Share" (not one after a decimal point, as "Rs 0 .70" leaves).
_AMOUNT = re.compile(
    r"R[se]\.?\s*-?\s*(?P<rupees>\d+(?:\.\d+)?)"
    r"|(?P<percent>\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)"
    r"|(?<![\d.])(?P<bare>\d+(?:\.\d+)?)\s*(?:/-\s*)?per\s*sh",
    re.IGNORECASE,
)
# A "/" that separates two parts, not the "/-" that ends an amount.
_PART = re.compile(r"/(?!\s*-)")
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Parsed:
    """A dividend subject read: each dividend's amount per share, in the order named."""

    amounts: tuple[Decimal, ...]

    @property
    def per_share(self) -> Decimal:
        return sum(self.amounts, Decimal(0))


@dataclass(frozen=True)
class Unparsed:
    """A dividend subject that could not be read, and why; it credits nothing."""

    reason: str


def names_dividend(subject: str) -> bool:
    """Whether a subject names a dividend (or a unit's distribution), of any stored kind."""
    return bool(_DIVIDEND.search(subject) or _DISTRIBUTION.search(subject))


def read_dividend(subject: str, face_value: Decimal | None) -> Parsed | Unparsed:
    """The amounts per share ``subject`` states, a percent of face value read against
    ``face_value``; or why they cannot be read."""
    if _UNIT.search(subject):
        return Unparsed("a distribution per unit, not a dividend per share")
    if _INCLUDING.search(subject):
        return Unparsed("states a dividend included in another")
    amounts: list[Decimal] = []
    for part in _PART.split(subject):
        found = list(_AMOUNT.finditer(part))
        named = len(_DIVIDEND.findall(part))
        if found and not named:
            return Unparsed(f"an amount in a part naming no dividend: {part.strip()!r}")
        if len(found) != named:
            return Unparsed(
                f"{named} dividend{'s' if named != 1 else ''} and {len(found)}"
                f" amount{'s' if len(found) != 1 else ''} in {part.strip()!r}"
            )
        for match in found:
            if match["percent"] is not None:
                if face_value is None:
                    return Unparsed("a percent of face value, and no face value is stored")
                amount = Decimal(match["percent"]) * face_value / _HUNDRED
            else:
                amount = Decimal(match["rupees"] or match["bare"])
            if amount <= 0:
                return Unparsed(f"an amount of zero in {part.strip()!r}")
            amounts.append(amount)
    if not amounts:
        return Unparsed("names no dividend")
    return Parsed(tuple(amounts))


@dataclass(frozen=True)
class Dividend:
    """One dividend subject of an Instrument on its Ex-Date, read or not."""

    instrument: str
    symbol: str
    ex_date: date
    subject: str
    face_value: Decimal | None
    reading: Parsed | Unparsed

    @property
    def per_share(self) -> Decimal | None:
        """The cash a share is credited; None when the subject is unparsed."""
        return self.reading.per_share if isinstance(self.reading, Parsed) else None

    @property
    def reason(self) -> str | None:
        return self.reading.reason if isinstance(self.reading, Unparsed) else None


def dividends(instrument: str, actions: Iterable[CorporateAction]) -> list[Dividend]:
    """Each dividend ``actions`` name for ``instrument``, one per Ex-Date and subject (NSE
    lists one under each series), oldest first."""
    found: dict[tuple[date, str], Dividend] = {}
    for a in sorted(actions, key=lambda a: (a.ex_date, a.subject, a.series)):
        if not names_dividend(a.subject):
            continue
        found.setdefault(
            (a.ex_date, a.subject),
            Dividend(
                instrument,
                a.symbol,
                a.ex_date,
                a.subject,
                a.face_value,
                read_dividend(a.subject, a.face_value),
            ),
        )
    return list(found.values())


@dataclass(frozen=True)
class DividendCheck:
    """Every dividend subject of some Instruments over a span: those read, and those not,
    each with its reason."""

    parsed: tuple[Dividend, ...]
    unparsed: tuple[Dividend, ...]

    def summary(self) -> dict[str, object]:
        return {
            "parsed": len(self.parsed),
            "unparsed": len(self.unparsed),
            "unparsed_subjects": [
                {
                    "instrument": d.instrument,
                    "symbol": d.symbol,
                    "ex_date": d.ex_date.isoformat(),
                    "subject": d.subject,
                    "reason": d.reason,
                }
                for d in self.unparsed
            ],
        }


def dividend_check(
    conn: psycopg.Connection, instruments: Sequence[str], *, start: date, end: date
) -> DividendCheck:
    """Every dividend subject ex-dated from ``start`` to ``end`` of each of ``instruments``
    (Instrument keys, or Symbols read as of ``end``), parsed or listed with its reason."""
    found = [
        d
        for instrument in sorted(set(instruments))
        for d in dividends(
            instrument,
            queries.corporate_actions(conn, instrument, start=start, end=end, as_of=end),
        )
    ]
    return DividendCheck(
        parsed=tuple(d for d in found if d.reason is None),
        unparsed=tuple(d for d in found if d.reason is not None),
    )


LISTED_COLUMNS = ("symbol", "ex_date", "subject", "reason")


@dataclass(frozen=True)
class ListedDividend:
    """A reviewed row: an unparsed dividend subject, and the Operator's reason why it is
    credited nothing (a fund's distribution, a subject NSE stated no amount in)."""

    symbol: str
    ex_date: date
    subject: str
    reason: str
    # The row's line in the file, for naming it.
    line: int = 0

    def key(self) -> tuple[str, date, str]:
        return self.symbol, self.ex_date, self.subject


def load_listed_dividends(path: Path) -> tuple[ListedDividend, ...]:
    """The reviewed list of unparsed dividend subjects at ``path``; a row without a reason,
    or listed twice, refuses the whole list."""
    lines = read_lines(path, "unparsed dividend list")
    require_header(path, next(iter(lines), None), LISTED_COLUMNS, "first")
    rows, seen = [], FirstLines[tuple[str, date, str]]()
    for line in lines[1:]:
        symbol, ex, subject, reason = line.columns(LISTED_COLUMNS)
        where = f"{line.where}: {symbol}"
        if not symbol or not subject:
            raise ValueError(f"{line.where}: the Symbol and the subject are required")
        if not reason:
            raise ValueError(f"{where}: no reason; say why the subject is credited nothing")
        row = ListedDividend(symbol, parse_date(where, "ex_date", ex), subject, reason, line.number)
        seen.add(row.key(), line, "the subject is listed already")
        rows.append(row)
    return tuple(rows)
