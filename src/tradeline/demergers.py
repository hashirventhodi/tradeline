"""The demerger table and the Demerger Credits it gives a holder (ADR 0020).

A demerger moves the parent's price without a split or bonus factor, so the
Adjusted Series only flags it (ADR 0006). A holder is credited in cash
instead, from a reviewed, checked-in table, at the location
``corporate_actions.demergers`` in ``tradeline.toml``, each row reviewed like
code::

    parent,child,ratio,ex_date,listed_on,parent_basis,child_basis,citation
    ITC,ITCHOTELS,1:10,2025-01-06,2025-01-29,86.49,13.51,"NSE/CMTR/65845; https://..."

``parent`` and ``child`` are NSE Symbols, the parent's as of the close before
the Ex-Date and the child's on its first listing day. ``ratio`` is the
scheme's entitlement, ``<child shares>:<parent shares>`` (1 ITC Hotels share
for 10 ITC is ``1:10``). ``listed_on`` is the child's first day in NSE's
end-of-day files, in any equity listing series (BE included). ``parent_basis``
and ``child_basis`` are the company's announced apportionment of the parent's
cost of acquisition, in percent, summing to 100; both are left empty when none
was announced, and the child's share is then zero (which overstates tax).
``citation`` is one or more sources separated by ``;``, each an NSE circular
number or an ``https://`` URL (NSE's or the company's filing). Lines starting
with ``#`` are comments, and blank lines are skipped. A row without a
citation, or whose ratio, basis or dates do not parse, refuses the whole
table, naming the line.

``tradeline ingest demergers`` stores the rows; a Protocol that credits
demergers freezes the stored rows' ``hash`` in its data snapshot
(``tradeline.freeze``), so an edit stored after the freeze refuses its runs.

The credit itself (``tradeline.engine``): a Position held at the close before
the Ex-Date is credited, on ``listed_on``, its shares then times ``ratio``
times the child's close that day in the Raw Series, rounded to the paisa (a
fractional entitlement is valued as the whole ones are, as NSE's trustee sells
the fractions). The child's share of the Position's basis moves to the credit,
which the tax ledger takes as a sale against it (``tax.DemergerCredit``), and
the Position's basis is reduced by it (``queries.position_bases``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from tradeline.reviewed_csv import parse_date, read_lines, require_header

COLUMNS = (
    "parent",
    "child",
    "ratio",
    "ex_date",
    "listed_on",
    "parent_basis",
    "child_basis",
    "citation",
)
_RATIO = re.compile(r"(\d+):(\d+)")
# An NSE (or NSE Clearing) circular number, or any https URL: NSE's or the company's filing.
_SOURCE = re.compile(r"(?:NSE|NCL)/[A-Z]+/\d+|https://\S+")
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Demerger:
    """A reviewed row: a parent's shares entitle a holder to the child's at ``ratio``."""

    parent: str
    child: str
    # Child shares per parent share.
    ratio: Fraction
    ex_date: date
    listed_on: date
    # The announced apportionment of the parent's cost, in percent; 100 and 0 if none.
    parent_basis: Decimal
    child_basis: Decimal
    citation: str
    # The row's line in the file, for naming it in a refusal.
    line: int = 0

    def key(self) -> str:
        """The row as the table's hash reads it: every field but its line."""
        return "|".join(
            (
                self.parent,
                self.child,
                f"{self.ratio.numerator}/{self.ratio.denominator}",
                self.ex_date.isoformat(),
                self.listed_on.isoformat(),
                f"{self.parent_basis.normalize():f}",
                f"{self.child_basis.normalize():f}",
                self.citation,
            )
        )


@dataclass(frozen=True)
class DemergerTable:
    rows: tuple[Demerger, ...]

    @property
    def hash(self) -> str:
        return table_hash(self.rows)


def table_hash(rows: tuple[Demerger, ...] | list[Demerger]) -> str:
    """SHA-256 of the rows in (Ex-Date, parent, child) order, comments and lines aside."""
    ordered = sorted(rows, key=lambda d: (d.ex_date, d.parent, d.child))
    return hashlib.sha256("\n".join(d.key() for d in ordered).encode()).hexdigest()


def load_demergers(path: Path) -> DemergerTable:
    """Every row of the demerger table at ``path``, oldest Ex-Date first; any bad row
    refuses the table."""
    lines = read_lines(path, "demerger table")
    require_header(path, next(iter(lines), None), COLUMNS, "first")
    rows = []
    for line in lines[1:]:
        parent, child, ratio, ex, listed, parent_basis, child_basis, citation = line.columns(
            COLUMNS
        )
        if not parent or not child:
            raise ValueError(f"{line.where}: the parent and the child Symbols are required")
        where = f"{line.where}: {parent}"
        found = _RATIO.fullmatch(ratio)
        if found is None or 0 in (int(found[1]), int(found[2])):
            raise ValueError(
                f"{where}: ratio {ratio!r} is not <child shares>:<parent shares>, as 1:10"
            )
        ex_date = parse_date(where, "ex_date", ex)
        listed_on = parse_date(where, "listed_on", listed)
        if listed_on < ex_date:
            raise ValueError(f"{where}: the child is listed before the Ex-Date {ex_date}")
        if not citation:
            raise ValueError(f"{where}: no citation; cite NSE's circular or the company's filing")
        for reference in citation.split(";"):
            if not _SOURCE.fullmatch(reference.strip()):
                raise ValueError(
                    f"{where}: {reference.strip()!r} is not an NSE circular or an https URL"
                )
        parent_share, child_share = _basis(where, parent_basis, child_basis)
        rows.append(
            Demerger(
                parent,
                child,
                Fraction(int(found[1]), int(found[2])),
                ex_date,
                listed_on,
                parent_share,
                child_share,
                citation,
                line.number,
            )
        )
    return DemergerTable(tuple(sorted(rows, key=lambda d: (d.ex_date, d.parent, d.child))))


def _basis(where: str, parent: str, child: str) -> tuple[Decimal, Decimal]:
    if not parent and not child:
        return _HUNDRED, Decimal(0)
    if not parent or not child:
        raise ValueError(f"{where}: state the parent's and the child's basis both or neither")
    try:
        shares = Decimal(parent), Decimal(child)
    except InvalidOperation:
        raise ValueError(f"{where}: the basis {parent!r} / {child!r} is not two numbers") from None
    if any(not s.is_finite() or s < 0 for s in shares) or sum(shares) != _HUNDRED:
        raise ValueError(f"{where}: the basis {parent} / {child} does not sum to 100")
    return shares
