"""``compare-adjusted``: our Adjusted Series against Kite's candles, bar by bar.

Kite's candles are the broker's displayed adjusted chart as of the day they
were fetched (docs/research.md). For each named symbol, every trading day in
the range is compared in series EQ: our adjusted bar
(``queries.adjusted_daily_candles`` as of ``as_of``, normally the fetch day)
against the stored candle, on open, high, low, and close to the paisa.
Volume is not compared: a match is about price.

A bar on one side only is listed by day. The symbol's corporate actions with
an Ex-Date in the range or after it, up to ``as_of``, are listed with the
result, since those are what either side adjusted for.

Mismatches are grouped into stretches: runs of mismatched bars with no
matching bar and no Ex-Date between them. A stretch's factor is the median of
Kite's close over ours across it, to four places. Kite adjusts for some
actions we do not (dividends of about 2 percent or more), and between two
Ex-Dates its adjustment is one constant, so a stretch that ends at an Ex-Date
at a steady factor is explained by that action.

Nothing is written: this is a read.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.adjust import ADJUSTED_KINDS, CorporateAction

SERIES = "EQ"
FACTOR_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class Prices:
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None


@dataclass(frozen=True)
class BarMismatch:
    trade_date: date
    kite: Prices
    ours: Prices


@dataclass(frozen=True)
class Stretch:
    """Consecutive mismatched bars, with no matching bar and no Ex-Date between them."""

    first: date
    last: date
    bars: int
    # The median of Kite's close over ours, to four places; None if no bar has both closes.
    factor: Decimal | None


@dataclass(frozen=True)
class SymbolComparison:
    symbol: str
    # Days with a bar on both sides, oldest first.
    compared: tuple[date, ...]
    mismatches: tuple[BarMismatch, ...]
    only_kite: tuple[date, ...]
    only_ours: tuple[date, ...]
    # Every action ex-dated from the range start to the as-of date, oldest first.
    actions: tuple[CorporateAction, ...]

    @property
    def stretches(self) -> tuple[Stretch, ...]:
        mismatched = {m.trade_date: m for m in self.mismatches}
        ex_dates = sorted({a.ex_date for a in self.actions})
        runs: list[list[BarMismatch]] = []
        previous: date | None = None
        for day in self.compared:
            if day in mismatched:
                # An Ex-Date after the previous bar, up to this one, starts a new stretch.
                crossed = previous is not None and any(previous < d <= day for d in ex_dates)
                if previous not in mismatched or crossed:
                    runs.append([])
                runs[-1].append(mismatched[day])
            previous = day
        return tuple(_stretch(run) for run in runs)

    @property
    def listed_actions(self) -> tuple[CorporateAction, ...]:
        """``actions`` without meetings, buybacks, and the rest NSE lists that adjust nothing."""
        return tuple(a for a in self.actions if a.kind != "other")

    @property
    def adjusting_actions(self) -> tuple[CorporateAction, ...]:
        """The splits and bonuses among ``actions``: what our series adjusts for."""
        return tuple(a for a in self.actions if a.kind in ADJUSTED_KINDS)

    @property
    def bars(self) -> int:
        return len(self.compared)

    @property
    def matched(self) -> int:
        return self.bars - len(self.mismatches)

    @property
    def matches(self) -> bool:
        return self.bars > 0 and not (self.mismatches or self.only_kite or self.only_ours)

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "matched": self.matched,
            "mismatched": len(self.mismatches),
            "matches": self.matches,
            "stretches": [
                {
                    "first": s.first.isoformat(),
                    "last": s.last.isoformat(),
                    "bars": s.bars,
                    "factor": None if s.factor is None else str(s.factor),
                }
                for s in self.stretches
            ],
            "only_kite": [d.isoformat() for d in self.only_kite],
            "only_ours": [d.isoformat() for d in self.only_ours],
            "actions": [
                {"ex_date": a.ex_date.isoformat(), "kind": a.kind, "subject": a.subject}
                for a in self.listed_actions
            ],
        }


@dataclass(frozen=True)
class AdjustedComparison:
    start: date
    end: date
    as_of: date
    symbols: tuple[SymbolComparison, ...]

    @property
    def passed(self) -> bool:
        return all(s.matches for s in self.symbols)

    def summary(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "as_of": self.as_of.isoformat(),
            "symbols_matching": sum(s.matches for s in self.symbols),
            "symbols": [s.summary() for s in self.symbols],
            "passed": self.passed,
        }

    def text(self) -> str:
        """One line per symbol, then a line per mismatched stretch and per action."""
        lines = [f"Adjusted series against Kite's candles, {self.start} to {self.end}:"]
        for s in self.symbols:
            verdict = "match" if s.matches else "MISMATCH"
            lines.append(f"  {s.symbol:<12} {s.matched}/{s.bars} bars equal  {verdict}")
            for stretch in s.stretches:
                lines.append(
                    f"    {stretch.first} to {stretch.last}: {stretch.bars} bars"
                    f" at Kite/ours {stretch.factor}"
                )
            if s.only_kite or s.only_ours:
                lines.append(f"    Kite only: {len(s.only_kite)}  ours only: {len(s.only_ours)}")
            for a in s.listed_actions:
                lines.append(f"    ex {a.ex_date} {a.kind}: {a.subject}")
        return "\n".join(lines) + "\n"


def compare_adjusted(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    as_of: date,
) -> AdjustedComparison:
    return AdjustedComparison(
        start=start,
        end=end,
        as_of=as_of,
        symbols=tuple(_compare(conn, s, start=start, end=end, as_of=as_of) for s in symbols),
    )


def _compare(
    conn: psycopg.Connection, symbol: str, *, start: date, end: date, as_of: date
) -> SymbolComparison:
    kite = {
        c.trade_date: c
        for c in queries.daily_candles(conn, symbol, start=start, end=end, series=SERIES)
    }
    ours = {
        b.trade_date: b
        for b in queries.adjusted_daily_candles(
            conn, symbol, start=start, end=end, as_of=as_of, series=SERIES
        )
    }
    mismatches = []
    both = sorted(kite.keys() & ours.keys())
    for day in both:
        k, o = kite[day], ours[day]
        kite_prices = Prices(k.open, k.high, k.low, k.close)
        our_prices = Prices(o.open, o.high, o.low, o.close)
        if kite_prices != our_prices:
            mismatches.append(BarMismatch(day, kite_prices, our_prices))
    actions = queries.corporate_actions(conn, symbol, start=start, end=as_of)
    return SymbolComparison(
        symbol=symbol,
        compared=tuple(both),
        mismatches=tuple(mismatches),
        only_kite=tuple(sorted(kite.keys() - ours.keys())),
        only_ours=tuple(sorted(ours.keys() - kite.keys())),
        actions=tuple(actions),
    )


def _stretch(run: list[BarMismatch]) -> Stretch:
    ratios = [m.kite.close / m.ours.close for m in run if m.kite.close and m.ours.close]
    factor = statistics.median(ratios).quantize(FACTOR_PLACES) if ratios else None
    return Stretch(run[0].trade_date, run[-1].trade_date, len(run), factor)
