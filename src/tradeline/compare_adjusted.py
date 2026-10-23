"""``compare-adjusted``: Factor Agreement, and our Adjusted Series against Kite's candles.

**Factor Agreement** (ADR 0018, amended by #68) judges each split and bonus
Ex-Date in the range of every ever-member's Instrument, or of each named Symbol's,
by Kite's Adjustment replayed in series EQ over the range
(``kite_adjustment.factor_agreement``): it agrees, disagrees, or is not judged
with a reason. The run passes when no Ex-Date disagrees. Kite's candles are the
broker's displayed chart as of the day they were fetched (docs/research.md).

For each named Symbol, every trading day in the range is also compared bar by
bar in series EQ: our adjusted bar (``queries.adjusted_daily_candles`` as of
``as_of``, normally the fetch day) against the stored candle, on open, high,
low, and close to the paisa. Volume is not compared: a match is about price.
The bars gate nothing: we adjust only splits and bonuses (ADR 0006), Kite also
dividends and demergers, and each of its stages rounds to its own tick.

A bar on one side only is listed by day. The symbol's corporate actions with
an Ex-Date in the range or after it, up to ``as_of``, are listed with the
result, since those are what either side adjusted for.

Mismatches are grouped into stretches: runs of mismatched bars with no
matching bar and no Ex-Date between them. A stretch's factor is the median of
Kite's close over ours across it, to four places. Between two Ex-Dates Kite's
adjustment is one constant, so a stretch that ends at an Ex-Date at a steady
factor is explained by that action.

Nothing is written: this is a read.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.adjust import ADJUSTED_KINDS, FLAGGED_KINDS, CorporateAction
from tradeline.kite_adjustment import Close, FactorJudgement, decimal_text, factor_agreement

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
        """``actions`` without meetings, buybacks, and the rest NSE lists that neither adjust
        nor flag anything."""
        return tuple(a for a in self.actions if a.kind in ADJUSTED_KINDS | FLAGGED_KINDS)

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
class InstrumentAgreement:
    """Factor Agreement for one Instrument: each split and bonus Ex-Date in the range."""

    instrument: str
    # The Symbols it is known by here, sorted.
    symbols: tuple[str, ...]
    judgements: tuple[FactorJudgement, ...]


@dataclass(frozen=True)
class AdjustedComparison:
    start: date
    end: date
    as_of: date
    # Bar by bar, for each named Symbol; none unless Symbols are named.
    symbols: tuple[SymbolComparison, ...]
    # Each Instrument with a split or bonus Ex-Date in the range.
    agreement: tuple[InstrumentAgreement, ...]

    @property
    def passed(self) -> bool:
        """Factor Agreement holds: no Ex-Date disagrees. Bars gate nothing."""
        return not any(j.outcome == "disagrees" for a in self.agreement for j in a.judgements)

    @property
    def judged(self) -> tuple[tuple[InstrumentAgreement, FactorJudgement], ...]:
        """Every Ex-Date judged, by Instrument then Ex-Date."""
        return tuple((a, j) for a in self.agreement for j in a.judgements)

    def summary(self) -> dict[str, Any]:
        outcomes = Counter(j.outcome for _, j in self.judged)
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "as_of": self.as_of.isoformat(),
            "factor_agreement": {
                "agrees": outcomes["agrees"],
                "disagrees": outcomes["disagrees"],
                "not_judged": outcomes["not_judged"],
                "not_judged_by_reason": dict(
                    sorted(Counter(j.reason for _, j in self.judged if j.reason).items())
                ),
                "ex_dates": [
                    {
                        "instrument": a.instrument,
                        "symbols": list(a.symbols),
                        "ex_date": j.ex_date.isoformat(),
                        "outcome": j.outcome,
                        "factor": decimal_text(j.factor),
                        "kite_factor": None
                        if j.kite_factor is None
                        else decimal_text(j.kite_factor),
                        "reason": j.reason,
                    }
                    for a, j in self.judged
                ],
            },
            "symbols_matching": sum(s.matches for s in self.symbols),
            "symbols": [s.summary() for s in self.symbols],
            "passed": self.passed,
        }

    def text(self) -> str:
        """Factor Agreement's counts and every Ex-Date not agreeing; then, for each named
        Symbol, a line, a line per mismatched stretch and one per action."""
        outcomes = Counter(j.outcome for _, j in self.judged)
        lines = [
            f"Factor Agreement, {self.start} to {self.end}: {len(self.judged)} split and bonus"
            f" Ex-Dates of {len(self.agreement)} Instruments: {outcomes['agrees']} agree,"
            f" {outcomes['disagrees']} disagree, {outcomes['not_judged']} not judged"
            f"  {'PASS' if self.passed else 'FAIL'}"
        ]
        for a, j in self.judged:
            name = "/".join(a.symbols)
            if j.outcome == "disagrees":
                lines.append(
                    f"  {name} {j.ex_date} DISAGREES: ours {decimal_text(j.factor)},"
                    f" Kite's {decimal_text(_of(j.kite_factor))}"
                )
            elif j.outcome == "not_judged":
                lines.append(f"  {name} {j.ex_date} not judged: {j.reason}")
        if self.symbols:
            lines.append(f"Adjusted series against Kite's candles, {self.start} to {self.end}:")
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
            for action in s.listed_actions:
                lines.append(f"    ex {action.ex_date} {action.kind}: {action.subject}")
        return "\n".join(lines) + "\n"


def compare_adjusted(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str] | None,
    start: date,
    end: date,
    as_of: date,
) -> AdjustedComparison:
    """Factor Agreement for every ever-member, or for the named Symbols with their bars."""
    if symbols is None:
        named: dict[str, set[str]] = {}
        for m in queries.universe_ever_members(conn):
            if m.instrument is not None:
                named.setdefault(m.instrument, set()).add(m.symbol)
    else:
        named = {s: {s} for s in symbols}
    return AdjustedComparison(
        start=start,
        end=end,
        as_of=as_of,
        symbols=tuple(_compare(conn, s, start=start, end=end, as_of=as_of) for s in symbols or ()),
        agreement=tuple(
            found
            for instrument in sorted(named)
            if (found := _agreement(conn, instrument, named[instrument], start=start, end=end))
            is not None
        ),
    )


def _agreement(
    conn: psycopg.Connection, instrument: str, symbols: set[str], *, start: date, end: date
) -> InstrumentAgreement | None:
    """The Instrument's split and bonus Ex-Dates in the range judged in series EQ, its
    whole history's actions replayed; None where it has none in the range."""
    actions = queries.corporate_actions(conn, instrument, as_of=end)
    if not any(a.kind in ADJUSTED_KINDS and start <= a.ex_date <= end for a in actions):
        return None
    nse = {
        r.trade_date: r.close
        for r in queries.end_of_day(conn, instrument, start=start, end=end, series=SERIES)
        if r.close  # NSE can state no close
    }
    candles = queries.instrument_daily_candles(
        conn, instrument, start=start, end=end, series=SERIES
    ).candles
    closes = [
        Close(c.trade_date, nse[c.trade_date], c.close) for c in candles if c.trade_date in nse
    ]
    judgements = factor_agreement(closes, actions, nse_closes=nse)
    return InstrumentAgreement(
        instrument=instrument,
        symbols=tuple(sorted(symbols)),
        judgements=tuple(j for j in judgements if start <= j.ex_date <= end),
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


def _of[T](value: T | None) -> T:
    if value is None:
        raise ValueError("a value was expected")
    return value
