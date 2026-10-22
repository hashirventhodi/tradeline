"""The shape of a reviewed CSV file checked into the repository, read whole or refused.

The Trading Calendar (``tradeline.trading_calendar``) and the Large Move records
(``tradeline.large_move_records``) are such files: lines starting with ``#``
are comments and blank lines are skipped, a header names the columns, and
every refusal names the file and the line, as ``<file> line N``, so a check
never runs on part of one.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Line:
    """A line of a reviewed file that is neither blank nor a comment."""

    number: int
    text: str
    # ``<file> line N``, to start a refusal of the line.
    where: str

    def fields(self) -> list[str]:
        return [f.strip() for f in next(csv.reader([self.text]))]

    def columns(self, columns: tuple[str, ...]) -> list[str]:
        """The line's fields, refused unless there is one for each of ``columns``."""
        fields = self.fields()
        if len(fields) != len(columns):
            raise ValueError(
                f"{self.where}: expected {len(columns)} columns ({','.join(columns)}): {self.text}"
            )
        return fields


def read_lines(path: Path, what: str) -> list[Line]:
    """The lines of the file at ``path`` that are neither blank nor comments, numbered from 1.

    ``what`` names the file in the refusal of one that does not exist."""
    if not path.exists():
        raise ValueError(f"{what} file {path} does not exist")
    return [
        Line(number, text, f"{path.name} line {number}")
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if text.strip() and not text.lstrip().startswith("#")
    ]


def require_header(path: Path, line: Line | None, columns: tuple[str, ...], which: str) -> None:
    """Refuse the file unless ``line``, its ``which`` row, names ``columns``."""
    if line is None or line.fields() != list(columns):
        raise ValueError(f"{path}: the {which} row must be the header {','.join(columns)}")


def parse_date(where: str, column: str, text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{where}: {column} {text!r} is not a date (YYYY-MM-DD)") from None


@dataclass
class FirstLines[K]:
    """The line each key was first given on: a second line giving one is refused."""

    _lines: dict[K, int] = field(default_factory=dict)

    def add(self, key: K, line: Line, repeat: str) -> None:
        """Note ``key`` on ``line``, or refuse the line as ``repeat`` if it is not the first."""
        if key in self._lines:
            raise ValueError(f"{line.where}: {repeat} on line {self._lines[key]}")
        self._lines[key] = line.number
