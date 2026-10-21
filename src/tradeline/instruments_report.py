"""``instruments``: the Operator's review of Instrument identity, in one place.

The report reads the last rebuild (``tradeline.rebuild_instruments``) and
writes nothing, so it can be run at any time:

- the anomalies: histories the builder split rather than joined, an
  unexplained ISIN change or a refused link (one that would have given an
  Instrument two live Symbols on a day);
- the Instruments that ended: those with no row on the last day of the Raw
  Series, each with its last day, last Symbol, and end reason (merged or
  delisted only on NSE's record, otherwise "reason not stated");
- the Universe's ever-members without Kite candles, by reason: series moved,
  merged, delisted, ended with no reason stated, or still trading (Kite lists
  no token for them), never "missing data";
- the link candidates: a new Symbol under an ISIN an earlier Symbol had, with
  no record joining them, for the Operator to cite in a manual link or leave;
- the stale Universe months, by rule version: months stored under a rule that
  ranks Instruments whose window a later rebuild regrouped. Their membership
  is kept as stored; recomputing one is the Operator's deliberate act.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

import psycopg

from tradeline import queries
from tradeline.instruments import Anomaly, LinkCandidate


@dataclass(frozen=True)
class InstrumentsReport:
    # The last day of the Raw Series the Instruments were built from; None before a build.
    last_day: date | None
    anomalies: tuple[Anomaly, ...]
    ended: tuple[queries.EndedInstrument, ...]
    # By reason, then key.
    without_candles: tuple[queries.EndReason, ...]
    candidates: tuple[LinkCandidate, ...]
    stale: tuple[queries.StaleUniverseMonth, ...]

    def text(self) -> str:
        """The human-readable report."""
        through = "no build" if self.last_day is None else f"through {self.last_day}"
        lines = [f"Instruments ({through})", "", f"Anomalies: {len(self.anomalies)}"]
        lines.extend(f"  {a.on}  {a.kind}  {a.symbol}  {a.detail}" for a in self.anomalies)
        lines += ["", f"Ended Instruments: {len(self.ended)}"]
        if self.ended:
            lines.append("  key  last day  last Symbol  reason")
            lines.extend(
                _joined(e.key, e.last_day, e.last_symbol, e.reason, e.detail) for e in self.ended
            )
        lines += ["", f"Members without Kite candles: {len(self.without_candles)}"]
        if self.without_candles:
            lines.append("  by reason: key  series  last day  last Symbol  detail")
        for reason, members in self._without_candles_by_reason().items():
            lines.append(f"  {reason}: {len(members)}")
            lines.extend(
                "  " + _joined(m.instrument, m.series, m.last_day, m.last_symbol, m.detail)
                for m in members
            )
        lines += ["", f"Link candidates: {len(self.candidates)}"]
        if self.candidates:
            lines.append("  old to new  ISIN  old's last day  new's first day")
            lines.extend(
                f"  {c.old_symbol} to {c.new_symbol}  {c.isin}  {c.old_last}  {c.new_first}"
                for c in self.candidates
            )
        lines += ["", f"Stale Universe months: {len(self.stale)}"]
        for version, months in self._stale_by_version().items():
            lines.append(f"  rule {version}: {', '.join(f'{m:%Y-%m}' for m in months)}")
        return "\n".join(lines) + "\n"

    def summary(self) -> dict[str, object]:
        """The machine-readable report."""
        return {
            "last_day": None if self.last_day is None else self.last_day.isoformat(),
            "anomalies": dict(sorted(Counter(a.kind for a in self.anomalies).items())),
            "ended": len(self.ended),
            "without_candles": {
                reason: len(members)
                for reason, members in self._without_candles_by_reason().items()
            },
            "candidates": len(self.candidates),
            "stale_months": {
                version: [f"{m:%Y-%m}" for m in months]
                for version, months in self._stale_by_version().items()
            },
        }

    def _without_candles_by_reason(self) -> dict[str, list[queries.EndReason]]:
        by_reason: dict[str, list[queries.EndReason]] = {}
        for member in self.without_candles:
            by_reason.setdefault(member.reason, []).append(member)
        return by_reason

    def _stale_by_version(self) -> dict[str, list[date]]:
        by_version: dict[str, list[date]] = {}
        for stale in self.stale:
            by_version.setdefault(stale.rule_version, []).append(stale.month)
        return by_version


def instruments_report(conn: psycopg.Connection) -> InstrumentsReport:
    return InstrumentsReport(
        last_day=queries.instruments_last_day(conn),
        anomalies=tuple(queries.instrument_anomalies(conn)),
        ended=tuple(queries.ended_instruments(conn)),
        without_candles=tuple(queries.members_without_candles(conn)),
        candidates=tuple(queries.link_candidates(conn)),
        stale=tuple(queries.stale_universe_months(conn)),
    )


def _joined(*fields: object) -> str:
    """A report line's fields, two spaces apart, an empty last one left off."""
    return "  " + "  ".join(str(f) for f in fields if f != "")
