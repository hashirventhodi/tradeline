"""Kite Parity: Kite's daily candles against NSE's end-of-day files (ADR 0018).

For every Instrument and series that has candles stored, each trading day in
the range is compared on presence and close, an end-of-day row and a candle
each belonging to the Instrument its Symbol named that day (ADR 0016). Kite
files an Instrument's whole history under its current name, so before a
rename its candles are under a Symbol NSE had not used yet; they belong to the
Instrument all the same. Of two candles of one Instrument-day under different
Kite names, the one under the more recent name is compared
(``queries.instrument_daily_candles``), and where the two differ the day is
reported as a **candle disagreement**, listed but not counted as a
difference: the compared candle is the one Kite serves today. A Symbol in no
Instrument (none rebuilt yet) stands for its own. Each series is compared on
its own; a candle on a day NSE lists its Instrument only in another listing
series is an extra, explained as a series move.

- **missing**: NSE has an end-of-day row and there is no candle;
- **extra**: there is a candle and NSE has no end-of-day row;
- **close**: both exist and the closes disagree beyond the tolerance.

Each difference is then given the first Parity Reason that explains it, tried
in the order ``REASONS`` lists for its kind, or is **unexplained**. A Parity
Reason is a rule over data we hold, never a list of dates, and a new one needs
ADR 0018 to agree it first. A missing candle on a day the Trading Calendar
(``parity.trading_calendar``, the file the Integrity Check reads) lists as a
Special Session is explained as one; a calendar that does not load refuses
the run, as it refuses the Integrity Check. A missing candle on a day outside
every range candle ingest asked Kite for, under any of the Instrument's
Symbols in that series (``queries.requested_candle_ranges``), is explained as
not fetched: history never requested is not Kite missing it.

A close mismatch is explained as **Kite's Adjustment** when replaying Kite's
stages from NSE's raw close gives exactly Kite's close (``tradeline.kite_adjustment``,
ADR 0018 as amended by #62): Kite adjusts its history one stage at a time, each
Ex-Date's stage dividing or subtracting from the history it has already rounded
and rounding again to its own tick. The stages are found once for each
Instrument and series that has a close mismatch, over all of its compared bars
in the range. Each Kite Basis they form that adjusts (one of Kite's raw
closes has no stages) is reported with its stages, the count of closes it
explains, and the closes it leaves unexplained; closes explained through a fitted
stage, one whose ratio is read from Kite's closes, are counted apart. Every
stretch left unexplained because a fitted stage could sit at more than one
Ex-Date, and every gap searched within a bound, is reported too. The summary
lists every Kite Basis; the text, the first ``MAX_LISTED``. Only a mismatch is
replayed: a close within the tolerance is never judged again.

A candle dated before its Instrument's Listing Age starts (the view
``instrument_listing_age``, ADR 0017) is a **Pre-listing Candle**: the
Instrument was not trading on NSE that day, so the candle cannot be its own.
Kite files history by instrument token, and a token can carry an earlier
security's bars (MAZDOCK's carries Farmax India's, docs/research.md, #34).
Such a candle is listed, with its count, but neither compared nor counted as
a row, and never attributed to another Instrument. A Symbol in no Instrument
has no Listing Age, so none of its candles is one.

Kite's candles are not raw prices: Kite adjusts its whole history, as of the
day it is fetched, for splits, bonuses, and large dividends (docs/research.md,
ADR 0006). Between two Ex-Dates that factor is one constant, so the closes
are compared after taking it out: the factor of a stretch of days is the
median of Kite's close over NSE's across it, the stretches being cut at every
Ex-Date NSE lists for any of its Symbols (any kind, since Kite's rules for which
actions it adjusts are its own). The median is robust to a few bad closes;
a stretch of one or two non-zero closes cannot show a bad one at all. A close
mismatches when it is further from NSE's close times the factor than
``close_tolerance`` of that price plus half Kite's 5 paisa tick, since Kite
rounds its adjusted prices to the tick. Bars of one stretch fetched on
different days, either side of a new action, carry different factors and are
reported as mismatches, which they are.

Kite's close can be 0: an adjusted price rounds to 0 when it is under half
the tick, or under 0.50 where Kite rounds an instrument's history to whole
rupees (#17, #41). Such a bar is left out of its stretch's factor, a ratio of
0 showing only that the price rounded away, and is still compared against
NSE's close times the factor of the rest: it passes where that is within half
the tick of 0 and mismatches otherwise, so a whole-rupee 0 for 0.30 is a
mismatch, which Kite's Adjustment can explain. A stretch whose every close is 0 has
no factor: its bars count as rows and are never close mismatches, like a
stretch too short to show a bad close. Only the close is compared; a zero
open, high or low is ``compare-adjusted``'s to find.

The run passes when unexplained differences, of all three kinds, are under
``max_unexplained_pct`` of the rows compared, a row being one (Instrument,
series, day) present on either side; a run exactly at the threshold fails.
Nothing is written: parity is a read.
"""

from __future__ import annotations

import bisect
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, get_args

import psycopg

from tradeline import queries
from tradeline.config import Config
from tradeline.instruments import LISTING_SERIES
from tradeline.kite_adjustment import (
    AmbiguousFit,
    BoundedSearch,
    Close,
    KiteBasis,
    Replay,
    Stage,
    replay,
)
from tradeline.trading_calendar import load_trading_calendar

Kind = Literal["missing", "extra", "close"]
KINDS: tuple[Kind, ...] = get_args(Kind)
HALF_TICK = Decimal("0.025")
HUNDRED = Decimal(100)
# Every difference is counted; this many unexplained ones are listed in a summary.
MAX_LISTED = 100


class Reason(StrEnum):
    """A Parity Reason: a stated, checkable cause of a difference (ADR 0018).

    Each is a member, with its rule in ``_rules`` and its place in
    ``REASONS``.
    """

    # A candle on a day NSE lists its Instrument in another listing series and
    # not in the candle's: Kite's token carrying on through BE or BZ days.
    SERIES_MOVE = "series_move"
    # A missing candle on a day the Trading Calendar lists as a Special Session.
    SPECIAL_SESSION = "special_session"
    # A missing bar on a day no range candle ingest asked Kite for covers.
    NOT_FETCHED = "not_fetched"
    # Kite's close is what replaying Kite's stages gives from NSE's raw close.
    KITES_ADJUSTMENT = "kites_adjustment"


# Each kind's Parity Reasons, in the order they are tried; the first that applies is
# recorded. ADR 0018's order: missing, a Special Session then not fetched; extra, a
# series move; close, Kite's Adjustment.
REASONS: Mapping[Kind, tuple[Reason, ...]] = {
    "missing": (Reason.SPECIAL_SESSION, Reason.NOT_FETCHED),
    "extra": (Reason.SERIES_MOVE,),
    "close": (Reason.KITES_ADJUSTMENT,),
}


@dataclass(frozen=True)
class Difference:
    kind: Kind
    symbol: str
    series: str
    trade_date: date
    candle_close: Decimal | None
    eod_close: Decimal | None
    # NSE's close times Kite's factor for the stretch; only for a close mismatch.
    expected_close: Decimal | None = None
    # The Parity Reason that explains it; None where it is unexplained.
    reason: Reason | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "series": self.series,
            "trade_date": self.trade_date.isoformat(),
            "candle_close": _text(self.candle_close),
            "eod_close": _text(self.eod_close),
            "expected_close": _text(self.expected_close),
            "reason": None if self.reason is None else self.reason.value,
        }


@dataclass(frozen=True)
class PreListingCandle:
    """A candle dated before its Instrument's Listing Age starts: never compared."""

    instrument: str
    symbol: str
    series: str
    trade_date: date
    close: Decimal
    # The day the Instrument's Listing Age starts (ADR 0017).
    listing_age_from: date

    def summary(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "symbol": self.symbol,
            "series": self.series,
            "trade_date": self.trade_date.isoformat(),
            "close": str(self.close),
            "listing_age_from": self.listing_age_from.isoformat(),
        }


@dataclass(frozen=True)
class ReplayedBasis:
    """A Kite Basis of one Instrument and series, with the close mismatches in its
    standing stretches it explains and leaves unexplained."""

    instrument: str
    series: str
    basis: KiteBasis
    explained: int
    # The day of each close mismatch in its standing stretches it leaves unexplained.
    unexplained_days: tuple[date, ...]

    @property
    def unexplained(self) -> int:
        return len(self.unexplained_days)

    def summary(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "series": self.series,
            "start": self.basis.start.isoformat(),
            "end": self.basis.end.isoformat(),
            "bars": self.basis.bars,
            "explains": self.basis.explains,
            "stages": [
                {
                    "ex_date": s.ex_date.isoformat(),
                    "how": "fitted" if s.fitted else "computed",
                    "form": s.form,
                    "divisor": _number(s.divisor),
                    "subtracted": _number(s.subtracted),
                    "tick": _number(s.tick),
                    "ties": s.ties,
                }
                for s in self.basis.stages
            ],
            "explained": self.explained,
            "unexplained": self.unexplained,
            "unexplained_days": [d.isoformat() for d in self.unexplained_days],
        }

    def text(self) -> str:
        stages = ", ".join(f"{s.ex_date} {s.form} {_does(s)}" for s in self.basis.stages)
        return (
            f"{self.instrument} {self.series} {self.basis.start} to {self.basis.end},"
            f" {self.basis.bars} bars{'' if self.basis.explains else ' (too few to explain)'}:"
            f" {stages}; {self.explained} explained, {self.unexplained} unexplained"
        )


def _does(stage: Stage) -> str:
    """What a stage does to each older close, in words: "divides by 2, rounds to 5 half_even"."""
    steps = [f"divides by {_number(stage.divisor)}"] if stage.multiplier != 1 else []
    if stage.subtracted:
        steps.append(f"subtracts {_number(stage.subtracted)}")
    steps.append(f"rounds to {_number(stage.tick)} {stage.ties}")
    return ", ".join(steps)


@dataclass(frozen=True)
class ParityReport:
    start: date
    end: date
    close_tolerance: Decimal
    max_unexplained_pct: Decimal
    rows: int
    # Every difference of each kind, explained or not, by kind.
    of_kind: Mapping[Kind, tuple[Difference, ...]]
    # Instrument-days stored under two Kite names with different values; not differences.
    candle_disagreements: tuple[queries.CandleDisagreement, ...] = ()
    # Candles from before their Instrument's Listing Age starts; not differences.
    pre_listing_candles: tuple[PreListingCandle, ...] = ()
    # Each Kite Basis with stages, of every Instrument and series with a close mismatch.
    kite_bases: tuple[ReplayedBasis, ...] = ()
    # Each replayed stretch left standing nowhere because a fitted stage's Ex-Date is
    # ambiguous, and each gap searched within a bound, with its Instrument and series.
    ambiguous_fits: tuple[tuple[str, str, AmbiguousFit], ...] = ()
    bounded_searches: tuple[tuple[str, str, BoundedSearch], ...] = ()

    @property
    def missing(self) -> tuple[Difference, ...]:
        return self.of_kind["missing"]

    @property
    def extra(self) -> tuple[Difference, ...]:
        return self.of_kind["extra"]

    @property
    def close_mismatches(self) -> tuple[Difference, ...]:
        return self.of_kind["close"]

    @property
    def differences(self) -> int:
        """Every difference of the three kinds, explained or not."""
        return sum(len(self.of_kind[kind]) for kind in KINDS)

    @property
    def unexplained(self) -> tuple[Difference, ...]:
        """The differences no Parity Reason explains: missing, then extra, then close."""
        return tuple(d for kind in KINDS for d in self.of_kind[kind] if d.reason is None)

    @property
    def unexplained_pct(self) -> Decimal:
        return HUNDRED * len(self.unexplained) / self.rows

    @property
    def passed(self) -> bool:
        """Unexplained differences strictly under the threshold."""
        return self.unexplained_pct < self.max_unexplained_pct

    @property
    def by_kind(self) -> dict[str, dict[str, int]]:
        """Each kind's differences: in all, under each of its reasons, and unexplained."""
        return {
            kind: {"total": len(self.of_kind[kind])}
            | {reason.value: _count(self.of_kind[kind], reason) for reason in REASONS[kind]}
            | {"unexplained": _count(self.of_kind[kind], None)}
            for kind in KINDS
        }

    @property
    def by_reason(self) -> dict[str, int]:
        """The differences under each Parity Reason, of any kind, then the unexplained."""
        every = [d for kind in KINDS for d in self.of_kind[kind]]
        return {reason.value: _count(every, reason) for reason in Reason} | {
            "unexplained": _count(every, None)
        }

    @property
    def kites_adjustment_through_fitted(self) -> int:
        """The closes Kite's Adjustment explains only through a fitted stage."""
        return sum(b.explained for b in self.kite_bases if b.basis.fitted)

    def summary(self) -> dict[str, Any]:
        """The machine-readable report: same data, same output, key for key."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "close_tolerance": str(self.close_tolerance),
            "max_unexplained_pct": str(self.max_unexplained_pct),
            "rows": self.rows,
            "differences": self.differences,
            "unexplained": len(self.unexplained),
            "unexplained_pct": _pct(self.unexplained_pct),
            "passed": self.passed,
            "by_kind": self.by_kind,
            "by_reason": self.by_reason,
            "listed_unexplained": [d.summary() for d in self.unexplained[:MAX_LISTED]],
            "kite_bases": len(self.kite_bases),
            "kites_adjustment_through_fitted": self.kites_adjustment_through_fitted,
            # Every one: the summary is where a Kite Basis the text leaves out is read.
            "listed_kite_bases": [b.summary() for b in self.kite_bases],
            "ambiguous_fits": [
                {
                    "instrument": instrument,
                    "series": series,
                    "start": a.stretch[0].isoformat(),
                    "end": a.stretch[1].isoformat(),
                    "ex_dates": [d.isoformat() for d in a.ex_dates],
                }
                for instrument, series, a in self.ambiguous_fits
            ],
            "bounded_searches": [
                {
                    "instrument": instrument,
                    "series": series,
                    "start": b.stretch[0].isoformat(),
                    "end": b.stretch[1].isoformat(),
                    "ex_dates": [d.isoformat() for d in b.ex_dates],
                    "layouts": b.layouts,
                    "stands": b.stands,
                }
                for instrument, series, b in self.bounded_searches
            ],
            "candle_disagreements": len(self.candle_disagreements),
            "listed_candle_disagreements": [
                _disagreement_summary(d) for d in self.candle_disagreements[:MAX_LISTED]
            ],
            "pre_listing_candles": len(self.pre_listing_candles),
            "listed_pre_listing_candles": [
                d.summary() for d in self.pre_listing_candles[:MAX_LISTED]
            ],
        }

    def text(self) -> str:
        """The human-readable report."""
        unexplained = self.unexplained
        lines = [
            f"parity {self.start} to {self.end}: {self.rows} rows,"
            f" {self.differences} differences ({len(self.missing)} missing,"
            f" {len(self.extra)} extra, {len(self.close_mismatches)} close),"
            f" {len(unexplained)} unexplained ({_pct(self.unexplained_pct)}%),"
            f" must be under {self.max_unexplained_pct}%: {'PASS' if self.passed else 'FAIL'}",
            "by reason: " + ", ".join(f"{name} {n}" for name, n in self.by_reason.items()),
        ]
        listed = unexplained[:MAX_LISTED]
        if listed:
            header = ("kind", "symbol", "series", "date", "candle", "eod", "expected")
            cells = [header] + [
                (
                    d.kind,
                    d.symbol,
                    d.series,
                    d.trade_date.isoformat(),
                    _text(d.candle_close) or "-",
                    _text(d.eod_close) or "-",
                    _text(d.expected_close) or "-",
                )
                for d in listed
            ]
            widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
            lines.append("")
            lines.append("unexplained:")
            lines.extend(
                "  ".join(
                    cell.ljust(width) for cell, width in zip(row, widths, strict=True)
                ).rstrip()
                for row in cells
            )
        if len(listed) < len(unexplained):
            lines.append(f"... {len(unexplained) - len(listed)} more not listed")
        if self.kite_bases:
            count = len(self.kite_bases)
            lines.append("")
            lines.append(
                f"{count} Kite Bas{'es' if count != 1 else 'is'} (Kite's Adjustment;"
                f" {self.kites_adjustment_through_fitted} closes explained only through a"
                " fitted stage):"
            )
            lines.extend(f"  {b.text()}" for b in self.kite_bases[:MAX_LISTED])
            if count > MAX_LISTED:
                lines.append(
                    f"... {count - MAX_LISTED} more not listed;"
                    " the JSON report lists every Kite Basis"
                )
        if self.ambiguous_fits:
            count = len(self.ambiguous_fits)
            lines.append("")
            lines.append(
                f"{count} ambiguous fit{'s' if count != 1 else ''} (a fitted stage would let"
                " the stretch stand at more than one Ex-Date; none is placed):"
            )
            lines.extend(
                f"  {instrument} {series} {a.stretch[0]} to {a.stretch[1]}:"
                f" {', '.join(str(d) for d in a.ex_dates)}"
                for instrument, series, a in self.ambiguous_fits
            )
        if self.bounded_searches:
            count = len(self.bounded_searches)
            lines.append("")
            lines.append(
                f"{count} bounded search{'es' if count != 1 else ''}"
                " (more layouts than are tried one by one):"
            )
            lines.extend(
                f"  {instrument} {series} {b.stretch[0]} to {b.stretch[1]}: {b.layouts} layouts"
                f" over {len(b.ex_dates)} Ex-Date{'s' if len(b.ex_dates) != 1 else ''},"
                f" {'stands' if b.stands else 'does not stand'}"
                for instrument, series, b in self.bounded_searches
            )
        if self.candle_disagreements:
            count = len(self.candle_disagreements)
            lines.append("")
            lines.append(
                f"{count} candle disagreement{'s' if count != 1 else ''}"
                " (one Instrument-day under two Kite names; the newer is compared):"
            )
            lines.extend(
                f"  {d.instrument} {d.series} {d.trade_date}: {d.kept.symbol} {d.kept.close},"
                f" {d.other.symbol} {d.other.close}"
                for d in self.candle_disagreements[:MAX_LISTED]
            )
            if count > MAX_LISTED:
                lines.append(f"... {count - MAX_LISTED} more not listed")
        if self.pre_listing_candles:
            count = len(self.pre_listing_candles)
            lines.append("")
            lines.append(
                f"{count} pre-listing candle{'s' if count != 1 else ''}"
                " (dated before its Instrument's Listing Age starts; not compared):"
            )
            lines.extend(
                f"  {c.instrument} {c.series} {c.trade_date}: {c.symbol} {c.close},"
                f" Listing Age from {c.listing_age_from}"
                for c in self.pre_listing_candles[:MAX_LISTED]
            )
            if count > MAX_LISTED:
                lines.append(f"... {count - MAX_LISTED} more not listed")
        return "\n".join(lines) + "\n"


def _count(differences: Sequence[Difference], reason: Reason | None) -> int:
    return sum(1 for d in differences if d.reason == reason)


def _disagreement_summary(d: queries.CandleDisagreement) -> dict[str, Any]:
    return {
        "instrument": d.instrument,
        "series": d.series,
        "trade_date": d.trade_date.isoformat(),
        "kept_symbol": d.kept.symbol,
        "kept_close": str(d.kept.close),
        "other_symbol": d.other.symbol,
        "other_close": str(d.other.close),
    }


@dataclass(frozen=True)
class _Row:
    # The Instrument's key, or the Symbol standing for one where it is in none.
    instrument: str
    # NSE's Symbol of the day, or Kite's where NSE has no row.
    symbol: str
    series: str
    trade_date: date
    candle_close: Decimal | None
    eod_close: Decimal | None
    has_candle: bool
    has_eod: bool
    # The day the Instrument's Listing Age starts; None for a Symbol in no Instrument.
    listing_age_from: date | None

    @property
    def pre_listing(self) -> bool:
        """A candle from before the Instrument was trading on NSE, with no row to meet.

        A day NSE has a row for is compared, whatever the last rebuild made of it."""
        return (
            self.has_candle
            and not self.has_eod
            and self.listing_age_from is not None
            and self.trade_date < self.listing_age_from
        )


@dataclass(frozen=True)
class _Unclassified:
    """A difference before it is classified, with what a Parity Reason's rule may read."""

    kind: Kind
    row: _Row
    # NSE's close times its stretch's factor, for a close mismatch; None otherwise.
    expected: Decimal | None = None


# A Parity Reason's rule: whether it explains a difference.
_Rule = Callable[[_Unclassified], bool]


def _rules(
    conn: psycopg.Connection,
    settings: Config.Parity,
    rows: Sequence[_Row],
    kites_adjustment: _KitesAdjustment,
) -> Mapping[Reason, _Rule]:
    """The rule of each Parity Reason, built once per run from the data it reads.

    A rule needing data beyond the compared rows (the Trading Calendar, the
    requested ranges, NSE's rows in other series) loads it here, once."""
    listed = _listed_series(conn, [r for r in rows if not r.has_eod])

    def series_move(difference: _Unclassified) -> bool:
        row = difference.row
        return bool(listed[(row.instrument, row.trade_date)] - {row.series})

    special_sessions = load_trading_calendar(Path(settings.trading_calendar)).special_sessions

    def special_session(difference: _Unclassified) -> bool:
        return difference.row.trade_date in special_sessions

    return {
        Reason.SERIES_MOVE: series_move,
        Reason.SPECIAL_SESSION: special_session,
        Reason.NOT_FETCHED: _not_fetched(conn, rows),
        Reason.KITES_ADJUSTMENT: kites_adjustment,
    }


class _KitesAdjustment:
    """Kite's Adjustment's rule: the close mismatch's day is one its Instrument and
    series's replay explains. Each is replayed once, at its first mismatch."""

    def __init__(self, conn: psycopg.Connection, rows: Sequence[_Row]) -> None:
        self._conn = conn
        self._closes: defaultdict[tuple[str, str], list[Close]] = defaultdict(list)
        # NSE's close of every day it states one, a candle or not: a dividend's P.
        self._nse: defaultdict[tuple[str, str], dict[date, Decimal]] = defaultdict(dict)
        for r in rows:
            if not r.eod_close:  # NSE can state no close
                continue
            self._nse[(r.instrument, r.series)][r.trade_date] = r.eod_close
            if r.candle_close is not None:
                self._closes[(r.instrument, r.series)].append(
                    Close(r.trade_date, r.eod_close, r.candle_close)
                )
        self.replays: dict[tuple[str, str], Replay] = {}

    def __call__(self, difference: _Unclassified) -> bool:
        row = difference.row
        key = (row.instrument, row.series)
        if key not in self.replays:
            actions = queries.corporate_actions(self._conn, row.instrument)
            self.replays[key] = replay(self._closes[key], actions, nse_closes=self._nse[key])
        return row.trade_date in self.replays[key].explained

    def bases(self, mismatches: Sequence[tuple[_Row, Reason | None]]) -> tuple[ReplayedBasis, ...]:
        """Each replayed Kite Basis with stages, with the close mismatches in it by reason."""
        found = []
        for (instrument, series), result in sorted(self.replays.items()):
            for basis in result.bases:
                if not basis.stages:
                    continue
                held = [
                    (row.trade_date, reason)
                    for row, reason in mismatches
                    if (row.instrument, row.series) == (instrument, series)
                    and basis.holds(row.trade_date)
                ]
                found.append(
                    ReplayedBasis(
                        instrument=instrument,
                        series=series,
                        basis=basis,
                        explained=sum(1 for _, r in held if r is Reason.KITES_ADJUSTMENT),
                        unexplained_days=tuple(sorted(d for d, r in held if r is None)),
                    )
                )
        return tuple(found)


def _listed_series(
    conn: psycopg.Connection, extras: Sequence[_Row]
) -> defaultdict[tuple[str, date], set[str]]:
    """The listing series NSE lists each extra's Instrument in on the extra's day.

    An end-of-day row belongs to the Instrument its Symbol named that day, so
    a row under any of the Instrument's Symbols counts, as in ``_rows``."""
    listed: defaultdict[tuple[str, date], set[str]] = defaultdict(set)
    if not extras:
        return listed
    found = conn.execute(
        "SELECT DISTINCT x.instrument, x.trade_date, b.series"
        " FROM unnest(%(instruments)s::text[], %(days)s::date[]) AS x(instrument, trade_date)"
        " JOIN eod_bhavcopy b ON b.trade_date = x.trade_date"
        " LEFT JOIN symbol_instruments m ON m.symbol = b.symbol"
        "  AND names_on(m.after, m.through, b.trade_date)"
        " WHERE coalesce(m.instrument_key, b.symbol) = x.instrument"
        "  AND b.series = ANY(%(series)s)",
        {
            "instruments": [r.instrument for r in extras],
            "days": [r.trade_date for r in extras],
            "series": list(LISTING_SERIES),
        },
    ).fetchall()
    for instrument, trade_date, series in found:
        listed[(instrument, trade_date)].add(series)
    return listed


def _not_fetched(conn: psycopg.Connection, rows: Sequence[_Row]) -> _Rule:
    """A missing bar on a day outside every range candle ingest asked Kite for, under
    any of the Instrument's Symbols, in that series: history never requested is not
    Kite missing it. Requests are recorded under the Symbol fetched, the Instrument's
    name at the time, so each of its Symbols is read."""
    wanted = sorted({(r.instrument, r.series) for r in rows if not r.has_candle})
    names: dict[str, set[str]] = defaultdict(set)
    for instrument, symbol in conn.execute(
        "SELECT instrument_key, symbol FROM symbol_instruments"
        " WHERE instrument_key = ANY(%s::text[])",
        ([instrument for instrument, _ in wanted],),
    ).fetchall():
        names[instrument].add(symbol)
    requested = {
        (instrument, series): [
            requested_range
            # A Symbol in no Instrument stands for its own.
            for symbol in sorted(names[instrument] or {instrument})
            for requested_range in queries.requested_candle_ranges(conn, symbol, series=series)
        ]
        for instrument, series in wanted
    }

    def rule(difference: _Unclassified) -> bool:
        row = difference.row
        ranges = requested[(row.instrument, row.series)]
        return not any(r.covers(row.trade_date) for r in ranges)

    return rule


def parity(
    conn: psycopg.Connection,
    settings: Config.Parity,
    *,
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
) -> ParityReport:
    """Compare stored candles with end-of-day rows from ``start`` to ``end`` inclusive.

    Every instrument with candles is compared unless ``symbols`` narrows it; a
    named symbol with no candles at all is refused, as is a range with nothing
    to compare, so an empty database cannot pass.
    """
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    names = None if symbols is None else sorted(set(symbols))
    if names is not None:
        _refuse_symbols_without_candles(conn, names)
    found = _rows(conn, start, end, names)
    if not found:
        raise ValueError(f"no candles or end-of-day rows to compare from {start} to {end}")
    rows = [r for r in found if not r.pre_listing]
    pre_listing = tuple(_pre_listing(r) for r in found if r.pre_listing)
    if not rows:
        count = len(pre_listing)
        raise ValueError(
            f"nothing to compare from {start} to {end}: only {count}"
            f" pre-listing candle{'s' if count != 1 else ''}"
        )

    tolerance = Decimal(str(settings.close_tolerance))
    kites_adjustment = _KitesAdjustment(conn, rows)
    rules = _rules(conn, settings, rows, kites_adjustment)

    def classified(differences: Sequence[_Unclassified]) -> tuple[Difference, ...]:
        return tuple(_classify(d, rules) for d in differences)

    mismatches = _close_mismatches(conn, rows, tolerance)
    close = classified(mismatches)

    return ParityReport(
        start=start,
        end=end,
        close_tolerance=tolerance,
        max_unexplained_pct=Decimal(str(settings.max_unexplained_pct)),
        rows=len(rows),
        of_kind={
            "missing": classified([_Unclassified("missing", r) for r in rows if not r.has_candle]),
            "extra": classified([_Unclassified("extra", r) for r in rows if not r.has_eod]),
            "close": close,
        },
        candle_disagreements=_candle_disagreements(conn, rows, start, end),
        pre_listing_candles=pre_listing,
        kite_bases=kites_adjustment.bases(
            [(u.row, d.reason) for u, d in zip(mismatches, close, strict=True)]
        ),
        ambiguous_fits=tuple(
            (instrument, series, a)
            for (instrument, series), r in sorted(kites_adjustment.replays.items())
            for a in r.ambiguous_fits
        ),
        bounded_searches=tuple(
            (instrument, series, b)
            for (instrument, series), r in sorted(kites_adjustment.replays.items())
            for b in r.bounded_searches
        ),
    )


def _classify(difference: _Unclassified, rules: Mapping[Reason, _Rule]) -> Difference:
    """The difference under the first of its kind's Parity Reasons that explains it."""
    reason = next((r for r in REASONS[difference.kind] if rules[r](difference)), None)
    row = difference.row
    return Difference(
        kind=difference.kind,
        symbol=row.symbol,
        series=row.series,
        trade_date=row.trade_date,
        candle_close=row.candle_close,
        eod_close=row.eod_close,
        expected_close=None
        if difference.expected is None
        else difference.expected.quantize(Decimal("0.01")),
        reason=reason,
    )


def _pre_listing(row: _Row) -> PreListingCandle:
    return PreListingCandle(
        instrument=row.instrument,
        symbol=row.symbol,
        series=row.series,
        trade_date=row.trade_date,
        close=_of(row.candle_close),
        listing_age_from=_of(row.listing_age_from),
    )


def _candle_disagreements(
    conn: psycopg.Connection, rows: list[_Row], start: date, end: date
) -> tuple[queries.CandleDisagreement, ...]:
    """Each compared Instrument's days stored under two Kite names that differ, from
    the day its Listing Age starts: a Pre-listing Candle's day is not the Instrument's."""
    listing = {r.instrument: r.listing_age_from or start for r in rows if r.has_candle}
    found = [
        d
        for instrument in sorted(listing)
        for d in queries.instrument_daily_candles(
            conn, instrument, start=max(start, listing[instrument]), end=end, as_of=date.max
        ).disagreements
    ]
    return tuple(found)


# The Instruments a name stands for: every one the Symbol has named, or the
# Symbol itself where it is in no Instrument (``candles_daily_instruments``).
_NAMED = (
    "(SELECT instrument_key FROM symbol_instruments WHERE symbol = ANY(%(names)s)"
    " UNION SELECT unnest(%(names)s::text[]))"
)


def _rows(conn: psycopg.Connection, start: date, end: date, names: list[str] | None) -> list[_Row]:
    # Keyed by (Instrument, series, day): an end-of-day row belongs to the
    # Instrument its Symbol named that day, like a candle (migration 0015). Of
    # two candles of one Instrument-day, the one under Kite's more recent name.
    found = conn.execute(
        "WITH instruments AS ("
        "  SELECT DISTINCT instrument, series FROM candles_daily_instruments"
        f" WHERE (%(names)s::text[] IS NULL OR instrument IN {_NAMED})"
        "), k AS ("
        "  SELECT DISTINCT ON (instrument, series, trade_date)"
        "   instrument, symbol, series, trade_date, close"
        "  FROM candles_daily_instruments"
        "  JOIN instruments USING (instrument, series)"
        "  WHERE trade_date BETWEEN %(start)s AND %(end)s"
        "  ORDER BY instrument, series, trade_date, named_until DESC NULLS LAST, symbol"
        "), e AS ("
        "  SELECT coalesce(m.instrument_key, b.symbol) AS instrument,"
        "   b.symbol, b.series, b.trade_date, b.close"
        "  FROM eod_bhavcopy b"
        "  LEFT JOIN symbol_instruments m ON m.symbol = b.symbol"
        "   AND names_on(m.after, m.through, b.trade_date)"
        "  JOIN instruments i"
        "   ON (i.instrument, i.series) = (coalesce(m.instrument_key, b.symbol), b.series)"
        "  WHERE b.trade_date BETWEEN %(start)s AND %(end)s"
        ")"
        " SELECT instrument, coalesce(e.symbol, k.symbol), series, trade_date, k.close, e.close,"
        "  k.instrument IS NOT NULL, e.instrument IS NOT NULL, la.listing_age_from"
        " FROM k FULL JOIN e USING (instrument, series, trade_date)"
        " LEFT JOIN instrument_listing_age la ON la.instrument_key = instrument"
        " ORDER BY instrument, series, trade_date",
        {"names": names, "start": start, "end": end},
    ).fetchall()
    return [_Row(*row) for row in found]


def _refuse_symbols_without_candles(conn: psycopg.Connection, names: list[str]) -> None:
    unknown = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM unnest(%(names)s::text[]) AS name"
            " WHERE NOT EXISTS (SELECT FROM candles_daily_instruments c"
            "  WHERE c.instrument = name OR c.instrument IN"
            "   (SELECT instrument_key FROM symbol_instruments WHERE symbol = name))"
            " ORDER BY name",
            {"names": names},
        ).fetchall()
    ]
    if unknown:
        raise ValueError(f"no candles stored for {', '.join(unknown)}")


def _close_mismatches(
    conn: psycopg.Connection, rows: list[_Row], tolerance: Decimal
) -> list[_Unclassified]:
    # Rows with both closes, grouped into the stretches between Ex-Dates.
    # An Instrument's Ex-Dates are those filed under any of its Symbols.
    grouped: dict[tuple[str, str, int], list[_Row]] = defaultdict(list)
    ex_dates: dict[str, list[date]] = {}
    for row in rows:
        if row.candle_close is None or not row.eod_close:  # NSE can state no close
            continue
        if row.instrument not in ex_dates:
            ex_dates[row.instrument] = sorted(
                {action.ex_date for action in queries.corporate_actions(conn, row.instrument)}
            )
        stretch_index = bisect.bisect_right(ex_dates[row.instrument], row.trade_date)
        grouped[(row.instrument, row.series, stretch_index)].append(row)

    mismatches: list[_Unclassified] = []
    for stretch_rows in grouped.values():
        # A zero close rounded away and shows no factor; a stretch of only those has none.
        ratios = [_ratio(r) for r in stretch_rows if r.candle_close]
        if not ratios:
            continue
        factor = statistics.median(ratios)
        for r in stretch_rows:
            expected = _of(r.eod_close) * factor
            if abs(_of(r.candle_close) - expected) > tolerance * expected + HALF_TICK:
                mismatches.append(_Unclassified("close", r, expected=expected))
    mismatches.sort(key=lambda d: (d.row.symbol, d.row.series, d.row.trade_date))
    return mismatches


def _ratio(row: _Row) -> Decimal:
    return _of(row.candle_close) / _of(row.eod_close)


def _of[T](value: T | None) -> T:
    if value is None:
        raise ValueError("a value was expected")
    return value


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _number(value: Fraction) -> str:
    """A ratio, amount or tick: exact where it ends within six decimals, else to six."""
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    shown = exact.quantize(Decimal("0.000001"))
    return f"{shown.normalize():f}"


def _pct(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))
