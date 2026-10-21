"""Exact symbol and company-name mentions in free text, for news items.

Built on ``tradeline.matching``, whose rules it applies to a headline or a
summary rather than to a filing's symbol and name fields:

- a symbol is mentioned where it appears as a whole token, case-sensitively
  as NSE writes it (``TCS``, ``BAJAJ-AUTO``, ``M&M``), a token boundary being
  any character other than a letter, a digit, ``&`` or ``-``; each token is
  resolved by ``SymbolMatcher.match(symbol=...)``;
- a company is mentioned where its name appears as a whole phrase, compared
  in ``matching.name_key`` form (case, punctuation and apostrophes, ``&``
  against ``and``) with the trailing ``Limited`` left off, since text says
  "Reliance Industries", not "Reliance Industries Limited". A possessive
  ("Infosys's") is read with and without its ``'s``. "Reliance" alone is not
  Reliance Industries.

A phrase that is the name of more than one symbol is ambiguous and matches
none. So is the name of an exchange ("BSE", "NSE", "MCX"): "BSE Sensex"
names the exchange's index, not BSE Ltd. A listed company whose symbol or
short name is an exchange's name is matched only by its full name with the
legal suffix ("BSE Ltd", "BSE Limited"), never by the bare token.

Names are found left to right, the longest first at each position, without
overlap, so a short name inside a longer matched one does not count.
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
_POSSESSIVE = re.compile(r"['’]s\b")
_SUFFIX = " limited"
_MIN_NAME_LENGTH = 2
# Exchange names, in ``name_key`` form: alone in text they name the exchange.
_EXCHANGES = frozenset({"bse", "nse", "mcx"})


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
                if phrase in _EXCHANGES:
                    # name_key reads "Ltd" as "Limited" only at the end of a
                    # name, so in running text both spellings are looked for.
                    for suffix in (_SUFFIX, " ltd"):
                        owners[phrase + suffix].add(symbol)
                elif len(phrase) >= _MIN_NAME_LENGTH:
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
            if name_key(token) not in _EXCHANGES
            and (symbol := self._symbols.match(symbol=token)) is not None
        }
        if self._pattern is not None:
            # name_key drops apostrophes, which keeps "Reddy's" whole but turns a
            # possessive "Infosys's" into "infosyss"; the text is read both ways.
            for form in (text, _POSSESSIVE.sub("", text)):
                found.update(
                    self._names[m.group(0)] for m in self._pattern.finditer(name_key(form))
                )
        return tuple(sorted(found))
