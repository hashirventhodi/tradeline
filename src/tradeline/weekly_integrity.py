"""The weekly Integrity Check over the data collected after a Protocol's freeze (ADR 0007,
2026-10-25 amendment).

Every day after the freeze that ``collect-daily`` loads is evidence for the Fresh Period,
and a missing one is a hole in it until refetched. NSE's archive serves the Raw Series and
Corporate Actions years later, so a gap is cheap to fix within a week and expensive when it
surfaces at the Fresh Period run. So each week the Integrity Check (``tradeline.integrity``)
runs over every day after the freeze to the day it runs, the whole span each time, so a gap
keeps alerting until it is refetched.

**The alert.** Every failure of the Integrity Check is one alert line naming its day (or
span, or Symbol): a missing or unexpected end-of-day file, days outside the Trading
Calendar, an ever-member no Instrument answers for, an unexplained Large Move. The command
logs them at ERROR and exits 1, which is where Phase 1's operational alerts go (the JSON
log the scheduler keeps); Telegram carries them from Phase 5.

**Kite Parity gaps.** A day an Instrument has an end-of-day row and no Kite candle is a
Kite Parity gap: listed with its Parity Reason, if one explains it, and never an Integrity
failure or an alert, since the Integrity Check never reads a broker (ADR 0018).

Nothing is written: the check is a read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg

from tradeline import queries
from tradeline.config import Config
from tradeline.dates import IST
from tradeline.integrity import MAX_LISTED, IntegrityReport, integrity
from tradeline.parity import Difference, parity


@dataclass(frozen=True)
class WeeklyIntegrityReport:
    # The days checked: the first after the freeze, to the day the check ran for.
    start: date
    end: date
    # None when no day after the freeze has come yet.
    integrity: IntegrityReport | None
    # Every day an Instrument has an end-of-day row and no candle; never an alert.
    parity_gaps: tuple[Difference, ...]

    @property
    def alerts(self) -> tuple[str, ...]:
        """One line for each failure of the Integrity Check, naming its day."""
        report = self.integrity
        if report is None:
            return ()
        lines = [
            _DAY_FAILURE[f.kind].format(day=f"{f.trade_date} {f.trade_date:%a}")
            + ("" if f.citation is None else f" (cited {f.citation})")
            for f in report.day_failures
        ]
        lines += [
            f"{s.start} to {s.end} lie outside the Trading Calendar; extend trading_calendar.csv"
            for s in report.uncovered
        ]
        lines += [
            f"ever-member {symbol} names no Instrument; run `tradeline rebuild-instruments`"
            for symbol in report.unresolved_members
        ]
        lines += [
            f"unexplained Large Move on {m.trade_date} {m.trade_date:%a}: {m.instrument}"
            f" ({m.symbol} {m.series}) {m.previous_close} to {m.close}"
            for m in report.unexplained_large_moves
        ]
        return tuple(lines)

    @property
    def passed(self) -> bool:
        return self.integrity is None or self.integrity.passed

    def summary(self) -> dict[str, Any]:
        """The machine-readable report: the same data as the text."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "passed": self.passed,
            "alerts": list(self.alerts),
            "kite_parity_gaps": [g.summary() for g in self.parity_gaps[:MAX_LISTED]],
            "kite_parity_gap_count": len(self.parity_gaps),
            "integrity": None if self.integrity is None else self.integrity.summary(),
        }

    def text(self) -> str:
        """The human-readable report: the alerts, the Kite Parity gaps, then the Integrity
        Check's own report."""
        lines = [
            f"weekly integrity {self.start} to {self.end}:"
            f" {'PASS' if self.passed else 'ALERT'}, {len(self.alerts)} alerts"
        ]
        lines += [f"  ALERT {alert}" for alert in self.alerts]
        lines.append(f"Kite Parity gaps (never Integrity failures): {len(self.parity_gaps)}")
        lines += [
            f"  Kite Parity gap {g.trade_date} {g.symbol} {g.series}"
            f" (never an Integrity failure){'' if g.reason is None else f', {g.reason.value}'}"
            for g in self.parity_gaps[:MAX_LISTED]
        ]
        if len(self.parity_gaps) > MAX_LISTED:
            lines.append(f"  ... and {len(self.parity_gaps) - MAX_LISTED} more")
        if self.integrity is None:
            return "\n".join(lines) + "\nnothing after the freeze to check yet\n"
        return "\n".join(lines) + "\n" + self.integrity.text()


_DAY_FAILURE = {
    "missing": "no end-of-day file for {day}, a trading day",
    "special_session_missing": "no end-of-day file for {day}, a listed Special Session",
    "holiday_with_file": "an end-of-day file for {day}, a listed holiday",
    "unlisted_weekend_file": "an end-of-day file for {day}, a weekend day the Trading"
    " Calendar does not list as a Special Session",
}


def weekly_integrity(
    conn: psycopg.Connection,
    settings: Config.Integrity,
    parity_settings: Config.Parity,
    *,
    after: date,
    end: date,
) -> WeeklyIntegrityReport:
    """The Integrity Check over the days after ``after`` (the freeze day) to ``end``, and
    the Kite Parity gaps over them; a read. Before the first day after the freeze, nothing
    is checked and nothing alerts."""
    start = after + timedelta(days=1)
    if end < start:
        return WeeklyIntegrityReport(start, end, None, ())
    report = integrity(conn, settings, start=start, end=end)
    gaps = parity(conn, parity_settings, start=start, end=end).missing
    return WeeklyIntegrityReport(start, end, report, gaps)


def frozen_on(conn: psycopg.Connection, protocol: str) -> date:
    """The day, in India, the Protocol ``protocol`` names (by at least 12 characters of its
    hash) was frozen; refused if none was. The Fresh Period's days come after it."""
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(f"no Protocol {protocol} is frozen")
    return frozen.frozen_at.astimezone(IST).date()
