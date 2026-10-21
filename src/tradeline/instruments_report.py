"""``instruments``: the Operator's review of Instrument identity, in one place.

The report reads the last rebuild (``tradeline.rebuild_instruments``) and
writes nothing, so it can be run at any time:

- the anomalies: histories the builder split rather than joined, an
  unexplained ISIN change or a refused link (one that would have given an
  Instrument two live Symbols on a day);
- the Instruments that ended: those with no row on the last day of the Raw
  Series, each with its last day and last Symbol;
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
    candidates: tuple[LinkCandidate, ...]
    stale: tuple[queries.StaleUniverseMonth, ...]

    def text(self) -> str:
        """The human-readable report."""
        through = "no build" if self.last_day is None else f"through {self.last_day}"
        lines = [f"Instruments ({through})", "", f"Anomalies: {len(self.anomalies)}"]
        lines.extend(f"  {a.on}  {a.kind}  {a.symbol}  {a.detail}" for a in self.anomalies)
        lines += ["", f"Ended Instruments: {len(self.ended)}"]
        if self.ended:
            lines.append("  key  last day  last Symbol")
            lines.extend(f"  {e.key}  {e.last_day}  {e.last_symbol}" for e in self.ended)
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
            "candidates": len(self.candidates),
            "stale_months": {
                version: [f"{m:%Y-%m}" for m in months]
                for version, months in self._stale_by_version().items()
            },
        }

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
        candidates=tuple(queries.link_candidates(conn)),
        stale=tuple(queries.stale_universe_months(conn)),
    )
