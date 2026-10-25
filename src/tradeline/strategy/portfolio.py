"""The portfolio Strategy interface: what a portfolio Strategy reads and what it returns.

A portfolio Strategy (ADR 0020) holds a set of names and changes it only at a
Rebalance or its account's Initial Buy (GLOSSARY). At each, it is given its
holdings, the Universe of the month containing the decision day and a
``MarketData`` bound to that day, and returns the holdings that leave and
the entrants, ranked best first, that fill the slots left (``rebalance``). It
says which months it Rebalances in (``rebalance_months``): the engine decides
on the last trading day of each. It sizes nothing, prices nothing and never
learns the Engine Mode; the engine splits the cash between the entrants and
the Risk Engine checks each buy (``tradeline.engine``, ``tradeline.risk``).

This module is part of the code revision of every Strategy that imports it
(``tradeline.revision``), and of no other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from tradeline.strategy import Candidate, MarketData, Member, Strategy


@dataclass(frozen=True)
class Rebalance:
    """A portfolio Strategy's decision: the held Instruments to sell, and the entrants to
    buy, each a Candidate at its rank in the whole ranking, best first."""

    leavers: tuple[str, ...]
    entrants: tuple[Candidate, ...]


@runtime_checkable
class PortfolioStrategy(Protocol):
    @property
    def family(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def ranking(self) -> str:
        """The ranking rule in words, recorded with the version."""
        ...

    @property
    def rebalance_months(self) -> tuple[int, ...]:
        """The months (1 to 12) whose last trading day is a Rebalance's decision day."""
        ...

    def parameters(self) -> Mapping[str, str]: ...

    def rebalance(
        self,
        holdings: Sequence[Member],
        universe: Sequence[Member],
        data: MarketData,
        as_of: date,
    ) -> Rebalance: ...


# Any Strategy the engine runs: one that screens and plans each trade, or a portfolio.
AnyStrategy = Strategy | PortfolioStrategy
