"""The Strategy interface: what every Strategy reads and what it returns.

A Strategy is given the month's Universe and a ``MarketData`` bound to the
Run's As-Of Date, and returns its Candidates ranked (``screen``); for each
Candidate it writes a Plan (``plan``). It never learns the Engine Mode, and the
data it is given cannot show it a bar or a Corporate Action after the As-Of
Date (ADR 0001, 0002, 0006).

A Strategy version is its family, its code revision and its Protocol. The code
revision is a hash of this package's parsed code (``tradeline.revision``), so
everything a Strategy's decisions depend on belongs in this package, and
nothing here may read a docstring: docstrings are left out of the hash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from tradeline.adjust import AdjustedBar


@dataclass(frozen=True)
class Member:
    """One Universe member as a Strategy sees it: the Instrument to read and its Symbol."""

    instrument: str
    symbol: str


@dataclass(frozen=True)
class Candidate:
    """An Instrument the screen surfaced: its rank (1 first), the score it was ranked by,
    and the values the screen read, which its Plan may use."""

    rank: int
    instrument: str
    symbol: str
    score: Decimal
    facts: Mapping[str, Decimal]


@dataclass(frozen=True)
class Plan:
    """A Candidate's trade as the Strategy prices it: entry limit, stop, target, and the
    time stop in trading days from the Fill."""

    rank: int
    instrument: str
    symbol: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    time_stop_days: int
    facts: Mapping[str, Decimal]


class MarketData(Protocol):
    """The Adjusted Series as it stood on ``as_of``; a read past it is refused."""

    @property
    def as_of(self) -> date: ...

    def bars(self, instrument: str, *, start: date, end: date) -> list[AdjustedBar]: ...

    def last_bars(self, instrument: str, count: int) -> list[AdjustedBar]:
        """The bars of the last ``count`` trading days up to ``as_of``; fewer if the
        Instrument did not trade on some."""
        ...


class Strategy(Protocol):
    @property
    def family(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def ranking(self) -> str:
        """The ranking rule in words, recorded with the version."""
        ...

    def parameters(self) -> Mapping[str, str]: ...

    def screen(
        self, universe: Sequence[Member], data: MarketData, as_of: date
    ) -> list[Candidate]: ...

    def plan(self, candidate: Candidate) -> Plan: ...
