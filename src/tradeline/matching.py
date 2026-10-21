"""Deterministic symbol matching against the instrument master.

An exchange filing or a news item names a company by an exchange symbol, a
company name, or both. It is matched to an NSE symbol only when that is
certain:

1. a symbol that is exactly a symbol in the master matches it;
2. otherwise a company name whose normalised form is exactly the normalised
   name of one symbol in the master matches that symbol;
3. anything else, including a name shared by two or more symbols (as Jain
   Irrigation's ordinary and DVR shares are), is unmatched.

Normalising a name removes only differences of spelling, never of identity:
case, punctuation (apostrophes are dropped, so "Reddy's" is "Reddys"), ``&``
against ``and``, runs of spaces, a leading ``The``, and a trailing ``Ltd``
against ``Limited`` ("INFOSYS LTD." is "Infosys Limited"). There is no
fuzzy or partial matching and no model call (Phase 1 spec).

The master is read once into a ``SymbolMatcher``; matching is then pure, so a
run matches every item against the same master.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

import psycopg

from tradeline import queries

_APOSTROPHE = re.compile(r"['’]")
_NOT_WORD = re.compile(r"[^0-9a-z]+")
_THE = re.compile(r"^the ")
_LTD = re.compile(r"\bltd$")


def name_key(name: str) -> str:
    """The form of a company name that two spellings of one name share."""
    text = _APOSTROPHE.sub("", name.casefold()).replace("&", " and ")
    text = " ".join(_NOT_WORD.sub(" ", text).split())
    return _LTD.sub("limited", _THE.sub("", text))


class SymbolMatcher:
    """Matches a filing's symbol or company name to one NSE symbol, or to none."""

    def __init__(self, listings: Iterable[tuple[str, str | None]]) -> None:
        """``listings`` is (symbol, company name) pairs; a symbol may repeat (one per series)."""
        self._symbols: set[str] = set()
        by_name: defaultdict[str, set[str]] = defaultdict(set)
        for symbol, name in listings:
            self._symbols.add(symbol)
            if name:
                by_name[name_key(name)].add(symbol)
        self._by_name = dict(by_name)

    @classmethod
    def load(cls, conn: psycopg.Connection) -> SymbolMatcher:
        """A matcher over the instrument master as it stands now."""
        return cls((s.symbol, s.name) for s in queries.symbols(conn))

    def match(self, *, symbol: str | None = None, name: str | None = None) -> str | None:
        """The one NSE symbol meant, or None when there is none or more than one."""
        if symbol is not None and symbol.strip() in self._symbols:
            return symbol.strip()
        if name is not None:
            candidates = self._by_name.get(name_key(name), set())
            if len(candidates) == 1:
                return next(iter(candidates))
        return None
