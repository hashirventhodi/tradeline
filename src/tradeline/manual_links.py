"""The manual Instrument Links: a checked-in file of renames NSE's list omits (ADR 0016).

NSE's symbol-change list misses some renames (RUCHI, now PATANJALI). The
Operator joins those by hand in a CSV file in the repository, at the location
``instruments.manual_links`` in ``tradeline.toml`` configures, and each row is
reviewed like code. A row is one rename::

    old_symbol,new_symbol,effective_date,citation
    RUCHI,PATANJALI,2022-07-13,https://archives.nseindia.com/content/circulars/CML52890.pdf

``effective_date`` is ISO (``YYYY-MM-DD``), the first day the new Symbol
trades, and ``citation`` an official source: an exchange circular's URL or
number, or a company filing. A row may also name one Symbol on both sides: an
ISIN change under that Symbol that no split or bonus explains (YESBANK's,
when the depositories applied its 2020 reconstruction scheme's lock-in),
effective the new ISIN's first day. Lines starting with ``#`` are comments, and blank
lines are skipped. The builder joins a manual link by the same rules as an NSE
record, and records the citation as the link's source.

A malformed row (the wrong number of columns, a blank Symbol, an unparseable
date) or a row without a citation refuses the
whole file, naming the line, so a rebuild never runs on part of it.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from tradeline.instruments import ManualLink

COLUMNS = ("old_symbol", "new_symbol", "effective_date", "citation")


def load_manual_links(path: Path) -> list[ManualLink]:
    """Every row of the manual-links file at ``path``; any bad row refuses the file."""
    if not path.exists():
        raise ValueError(f"manual links file {path} does not exist")
    lines = [
        (number, line)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or next(csv.reader([lines[0][1]])) != list(COLUMNS):
        raise ValueError(f"{path}: the first row must be the header {','.join(COLUMNS)}")
    return [_row(path, number, line) for number, line in lines[1:]]


def _row(path: Path, number: int, line: str) -> ManualLink:
    fields = [f.strip() for f in next(csv.reader([line]))]
    where = f"{path.name} line {number}"
    if len(fields) != len(COLUMNS):
        raise ValueError(f"{where}: expected {len(COLUMNS)} columns ({','.join(COLUMNS)}): {line}")
    old_symbol, new_symbol, effective, citation = fields
    if not old_symbol or not new_symbol:
        raise ValueError(f"{where}: both Symbols are required: {line}")
    where = f"{where}: {old_symbol} to {new_symbol}"
    try:
        effective_date = date.fromisoformat(effective)
    except ValueError:
        raise ValueError(
            f"{where}: effective_date {effective!r} is not a date (YYYY-MM-DD)"
        ) from None
    if not citation:
        raise ValueError(f"{where}: no citation; cite an official URL or circular number")
    return ManualLink(old_symbol, new_symbol, effective_date, citation)
