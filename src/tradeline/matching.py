"""Deterministic symbol matching for announcements and news.

A text is matched to an NSE symbol only on an exact mention:

- the symbol itself, as a whole token, case-sensitively as NSE writes it
  (``TCS``, ``BAJAJ-AUTO``, ``M&M``); a token boundary is any character other
  than a letter, a digit, ``&`` or ``-``;
- the company name from the instrument master, as a whole phrase,
  case-insensitively, after both are normalised (NFKC, lower case, every run
  of characters other than letters, digits and ``&`` collapsed to a space) and
  a trailing ``Limited`` or ``Ltd`` dropped from the name. "Reliance
  Industries" matches Reliance Industries Limited; "Reliance" alone does not.

A symbol or normalised name that belongs to more than one distinct symbol is
ambiguous and never matches. Names are found left to right, the longest name
first at each position, without overlap, so a short name inside a longer
matched one does not count. Anything else is left unmatched: no fuzzy
matching and no model calls.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from tradeline import queries

_SYMBOL_TOKEN = re.compile(r"[A-Za-z0-9&-]+")
_NOT_NAME_CHAR = re.compile(r"[^a-z0-9&]+")
_LEGAL_SUFFIXES = ("limited", "ltd")
_MIN_NAME_LENGTH = 2


@dataclass(frozen=True)
class Listing:
    symbol: str
    name: str | None


class SymbolMatcher:
    def __init__(self, listings: Iterable[Listing]) -> None:
        owners: dict[str, set[str]] = defaultdict(set)
        symbols: set[str] = set()
        for listing in listings:
            symbols.add(listing.symbol)
            if listing.name is not None:
                name = _normalise_name(listing.name)
                if len(name) >= _MIN_NAME_LENGTH:
                    owners[name].add(listing.symbol)
        self._symbols = frozenset(symbols)
        self._names = {name: next(iter(s)) for name, s in owners.items() if len(s) == 1}
        alternatives = sorted(self._names, key=lambda n: (-len(n), n))
        self._name_pattern = (
            re.compile(
                r"(?<![a-z0-9&])(?:" + "|".join(map(re.escape, alternatives)) + r")(?![a-z0-9&])"
            )
            if alternatives
            else None
        )

    def match(self, text: str) -> tuple[str, ...]:
        """The symbols ``text`` mentions exactly, sorted; empty when it names none."""
        text = unicodedata.normalize("NFKC", text)
        found = {token for token in _SYMBOL_TOKEN.findall(text) if token in self._symbols}
        if self._name_pattern is not None:
            normalised = _normalise_text(text)
            found.update(self._names[m.group(0)] for m in self._name_pattern.finditer(normalised))
        return tuple(sorted(found))


def matcher_from_master(conn: psycopg.Connection) -> SymbolMatcher:
    """A matcher over the instrument master, one listing per (symbol, series) row."""
    return SymbolMatcher(Listing(row.symbol, row.name) for row in queries.symbols(conn))


def _normalise_text(text: str) -> str:
    return _NOT_NAME_CHAR.sub(" ", unicodedata.normalize("NFKC", text).lower()).strip()


def _normalise_name(name: str) -> str:
    words = _normalise_text(name).split()
    if words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)
