"""Exact symbol and company-name mentions in free text, for news items.

Built on ``tradeline.matching``, whose rules it applies to a headline or a
summary rather than to a filing's symbol and name fields:

- a symbol is mentioned where it appears as a whole token, case-sensitively
  as NSE writes it (``TCS``, ``BAJAJ-AUTO``, ``M&M``), a token boundary being
  any character other than a letter, a digit, ``&`` or ``-``; each token is
  resolved by ``SymbolMatcher.match(symbol=...)``;
- a company is mentioned where its name appears as a whole phrase, compared
  in ``matching.name_key`` form (case, punctuation, ``&`` against ``and``)
  with the trailing ``Limited`` left off, since text says "Reliance
  Industries", not "Reliance Industries Limited". "Reliance" alone is not
  Reliance Industries.

A phrase that is the name of more than one symbol is ambiguous and matches
none. Names are found left to right, the longest first at each position,
without overlap, so a short name inside a longer matched one does not count.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable

import psycopg

from tradeline import queries
from tradeline.matching import SymbolMatcher, name_key

_SYMBOL_TOKEN = re.compile(r"[A-Za-z0-9&-]+")
_SUFFIX = " limited"
_MIN_NAME_LENGTH = 2


class TextMatcher:
    """The NSE symbols a piece of text mentions exactly."""

    def __init__(self, listings: Iterable[tuple[str, str | None]]) -> None:
        """``listings`` is (symbol, company name) pairs; a symbol may repeat (one per series)."""
        listings = list(listings)
        self._symbols = SymbolMatcher(listings)
        owners: defaultdict[str, set[str]] = defaultdict(set)
        for symbol, name in listings:
            if name:
                phrase = name_key(name).removesuffix(_SUFFIX)
                if len(phrase) >= _MIN_NAME_LENGTH:
                    owners[phrase].add(symbol)
        self._names = {phrase: next(iter(s)) for phrase, s in owners.items() if len(s) == 1}
        alternatives = sorted(self._names, key=lambda p: (-len(p), p))
        self._pattern = (
            re.compile(
                r"(?<![0-9a-z])(?:" + "|".join(map(re.escape, alternatives)) + r")(?![0-9a-z])"
            )
            if alternatives
            else None
        )

    @classmethod
    def load(cls, conn: psycopg.Connection) -> TextMatcher:
        """A matcher over the instrument master as it stands now."""
        return cls((s.symbol, s.name) for s in queries.symbols(conn))

    def match(self, text: str) -> tuple[str, ...]:
        """The symbols ``text`` mentions exactly, sorted; empty when it names none."""
        text = unicodedata.normalize("NFKC", text)
        found = {
            symbol
            for token in _SYMBOL_TOKEN.findall(text)
            if (symbol := self._symbols.match(symbol=token)) is not None
        }
        if self._pattern is not None:
            found.update(self._names[m.group(0)] for m in self._pattern.finditer(name_key(text)))
        return tuple(sorted(found))
