"""The cited Corporate Actions: a checked-in file of splits and bonuses NSE's lists omit.

Some splits and bonuses are in none of NSE's Corporate Action lists, though
NSE announced each by circular (ADR 0006, amended by #37): KARDA's 2020
split, two SME splits of 2025, and some ETF unit splits. The Operator records
each in a CSV file in the repository, at the location
``corporate_actions.cited`` in ``tradeline.toml`` configures, and each row is
reviewed like code::

    symbol,ex_date,subject,citation
    KARDA,2020-07-02,Sub-Division from Rs. 10/- to Rs. 2/-,"NSE/CML/44773; NCL/CMPT/44827"

``ex_date`` is ISO (``YYYY-MM-DD``) and must be stated by the circulars, never
inferred from prices or taken from a company's record date. ``subject`` is
NSE's own wording, from which ``tradeline.adjust.interpret`` derives the
factor, as it does for a listed action; a row never states a factor.
``citation`` is one or more NSE circulars, separated by ``;``, each a
circular number (``NSE/CML/44773``, ``NCL/CMPT/44827``) or its URL on
nseindia.com, which together state both the ratio and the Ex-Date. Lines
starting with ``#`` are comments, and blank lines are skipped.

A malformed row, a row without a citation or citing anything but NSE's
circulars, or a row whose subject gives no split or bonus factor refuses the
whole file, naming the line. ``ingest cited-actions``
(``tradeline.ingest.cited_actions``) checks each row against the price and
stores it.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path

from tradeline.adjust import ADJUSTED_KINDS, Kind, interpret

COLUMNS = ("symbol", "ex_date", "subject", "citation")
# An NSE (or NSE Clearing) circular number, or a circular's URL on nseindia.com.
_CIRCULAR = re.compile(
    r"(?:NSE|NCL)/[A-Z]+/\d+"
    r"|https?://(?:[a-z0-9]+\.)?nseindia\.com/content/circulars/[A-Za-z0-9_./-]+",
)


@dataclass(frozen=True)
class CitedAction:
    """A reviewed row: a split or bonus NSE's circulars state, with the factor its subject gives."""

    symbol: str
    ex_date: date
    subject: str
    citation: str
    kind: Kind
    factor: Fraction
    # The row's line in the file, for naming it in a report.
    line: int


def load_cited_actions(path: Path) -> list[CitedAction]:
    """Every row of the cited-actions file at ``path``; any bad row refuses the file."""
    if not path.exists():
        raise ValueError(f"cited corporate actions file {path} does not exist")
    lines = [
        (number, line)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or next(csv.reader([lines[0][1]])) != list(COLUMNS):
        raise ValueError(f"{path}: the first row must be the header {','.join(COLUMNS)}")
    return [_row(path, number, line) for number, line in lines[1:]]


def _row(path: Path, number: int, line: str) -> CitedAction:
    fields = [f.strip() for f in next(csv.reader([line]))]
    where = f"{path.name} line {number}"
    if len(fields) != len(COLUMNS):
        raise ValueError(f"{where}: expected {len(COLUMNS)} columns ({','.join(COLUMNS)}): {line}")
    symbol, ex, subject, citation = fields
    if not symbol or not subject:
        raise ValueError(f"{where}: the Symbol and the subject are required: {line}")
    where = f"{where}: {symbol}"
    try:
        ex_date = date.fromisoformat(ex)
    except ValueError:
        raise ValueError(f"{where}: ex_date {ex!r} is not a date (YYYY-MM-DD)") from None
    if not citation:
        raise ValueError(f"{where}: no citation; cite the NSE circulars stating ratio and Ex-Date")
    for reference in citation.split(";"):
        if not _CIRCULAR.fullmatch(reference.strip()):
            raise ValueError(
                f"{where}: {reference.strip()!r} is not an NSE circular;"
                " cite NSE circulars only, by number or nseindia.com URL"
            )
    kind, factor = interpret(subject)
    if kind not in ADJUSTED_KINDS or factor is None:
        raise ValueError(f"{where}: the subject {subject!r} gives no split or bonus factor")
    return CitedAction(symbol, ex_date, subject, citation, kind, factor, number)
