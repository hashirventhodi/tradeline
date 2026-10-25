"""Freezing a Protocol, and checking at run time that its data is still the rows there now.

``freeze_protocol`` states the Protocol (``tradeline.protocol``) from the
Operator's Terms, the configured Limits, Halt fractions and cost schedule, the
Strategy, the Universe Rule version and the git commit, and the data snapshot it reads from
the database: for each month from the first In-Sample day to the Holdout's
last, the month's mode (Recorded or Reconstructed) and its build, whose hashes
are checked against the rows stored now; and for every member of those months,
the hash of its Adjusted Series from the warm-up before the first Window to the
Holdout's last day, as of that day. The Protocol is written once under its
content hash; a second freeze of the same content writes nothing.

``verify_snapshot`` is the engine's check before a backtest: each Universe
month the backtest's range touches and each member of those months is hashed
again, and any that differs from the snapshot is named in the refusal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import psycopg

from tradeline import queries
from tradeline.costs import CostSchedule, Slippage
from tradeline.dates import IST
from tradeline.metrics import HaltLimits
from tradeline.protocol import (
    OBJECTIVE,
    TIE_BREAK,
    DataSnapshot,
    MonthSnapshot,
    Protocol,
    Terms,
    canonical,
    content_hash,
    months_between,
    window_boundaries,
)
from tradeline.revision import code_revision
from tradeline.risk import Limits
from tradeline.strategy import Strategy
from tradeline.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)


class SnapshotChanged(ValueError):
    """The data a Protocol froze is not the data stored now."""


@dataclass(frozen=True)
class FreezeResult:
    hash: str
    protocol: Protocol
    # False when the same Protocol was already frozen.
    written: bool

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": self.hash,
            "written": self.written,
            "windows": [
                {
                    "in_sample": [w.in_sample.first.isoformat(), w.in_sample.last.isoformat()],
                    "out_of_sample": [
                        w.out_of_sample.first.isoformat(),
                        w.out_of_sample.last.isoformat(),
                    ],
                }
                for w in self.protocol.windows
            ],
            "holdout": [
                self.protocol.holdout.first.isoformat(),
                self.protocol.holdout.last.isoformat(),
            ],
            "universe_months": len(self.protocol.snapshot.months),
            "instruments": len(self.protocol.snapshot.adjusted_series),
        }

    def text(self) -> str:
        state = "frozen" if self.written else "already frozen, nothing written"
        return f"Protocol {self.hash} ({state})\n{self.protocol.boundaries_text()}"


def freeze_protocol(
    conn: psycopg.Connection,
    terms: Terms,
    *,
    strategy: Strategy,
    rule_version: str,
    limits: Limits,
    halts: HaltLimits,
    schedule: CostSchedule,
    slippage: Slippage,
    calendar: TradingCalendar,
    commit: str,
    costs_as_of_for_tests: date | None = None,
) -> FreezeResult:
    """Freeze the Protocol these state, with the data snapshot read now, at ``commit``, its
    trades priced on ``schedule`` as of the freeze: the day it is frozen, in India.

    ``costs_as_of_for_tests`` replaces that day, so a test's hand-worked costs do not move
    with the day it runs; nothing else passes it."""
    # A Protocol states every Limit on or off (ADR 0020).
    limits.require_stated()
    costs_as_of = costs_as_of_for_tests or datetime.now(IST).date()
    parameters = set(strategy.parameters())
    if set(terms.grid) != parameters:
        raise ValueError(
            f"the grid's parameters {sorted(terms.grid)} are not {strategy.name}'s"
            f" {sorted(parameters)}"
        )
    windows, holdout = window_boundaries(terms.layout, calendar)
    first, last = windows[0].in_sample.first, holdout.last
    if not queries.end_of_day_dates(conn, start=last, end=last):
        raise ValueError(
            f"no end-of-day file is stored for {last}, the Holdout's last trading day;"
            " a Protocol freezes data that is there"
        )
    protocol = Protocol(
        family=strategy.family,
        name=strategy.name,
        code_revision=code_revision(type(strategy).__module__),
        git_commit=commit,
        grid=terms.grid,
        objective=OBJECTIVE,
        min_in_sample_trades=terms.min_in_sample_trades,
        tie_break=TIE_BREAK,
        windows=windows,
        holdout=holdout,
        costs_as_of=costs_as_of,
        rates=schedule.rates(costs_as_of),
        slippage=slippage,
        band=terms.band,
        limits=limits,
        halts=halts,
        starting_equity=terms.starting_equity,
        risk_free_rate=terms.risk_free_rate,
        gate=terms.gate,
        rule_version=rule_version,
        warm_up_trading_days=terms.warm_up_trading_days,
        snapshot=_data_snapshot(
            conn,
            rule_version,
            start=_warm_up_start(calendar, first, terms.warm_up_trading_days),
            first=first,
            last=last,
            calendar=calendar,
        ),
    )
    content = canonical(protocol.content())
    found = content_hash(protocol.content())
    with conn.transaction():
        written = conn.execute(
            "INSERT INTO protocols (hash, content) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (found, content),
        ).rowcount
    conn.commit()
    result = FreezeResult(found, protocol, written == 1)
    log.info(
        "protocol frozen" if result.written else "protocol already frozen",
        extra={"protocol": found},
    )
    return result


def verify_snapshot(
    conn: psycopg.Connection, frozen: queries.FrozenProtocol, *, start: date, end: date
) -> None:
    """Refuse, naming each, if a Universe month from ``start`` to ``end`` or the Adjusted
    Series of a member of one is not what the Protocol froze."""
    protocol = frozen.protocol
    by_month = {m.month: m for m in protocol.snapshot.months}
    differ: list[str] = []
    instruments: set[str] = set()
    for month in months_between(start, end):
        snapshot = by_month[month]
        hashes = queries.universe_window_hashes(
            conn, start=snapshot.window_start, cutoff=snapshot.data_cutoff
        )
        if hashes != (snapshot.eod_hash, snapshot.links_hash):
            differ.append(f"Universe month {month:%Y-%m}")
        instruments |= run_members(conn, month, protocol.rule_version)
    series = protocol.snapshot
    for instrument in sorted(instruments):
        now = queries.adjusted_series_hash(conn, instrument, start=series.start, end=series.end)
        if series.adjusted_series.get(instrument) != now:
            differ.append(f"Instrument {instrument}")
    if differ:
        raise SnapshotChanged(
            f"the data differs from what Protocol {frozen.hash[:12]} froze: "
            + ", ".join(differ)
            + "; a corrected input needs a new Protocol (`tradeline protocol freeze`)"
        )


def _data_snapshot(
    conn: psycopg.Connection,
    rule_version: str,
    *,
    start: date,
    first: date,
    last: date,
    calendar: TradingCalendar,
) -> DataSnapshot:
    months = []
    instruments: set[str] = set()
    for month in months_between(first, last):
        members = run_members(conn, month, rule_version)
        if not members:
            raise ValueError(
                f"no Universe for {month:%Y-%m} under rule version {rule_version[:12]};"
                " run `tradeline universe` for the month first"
            )
        builds = queries.universe_month_builds(conn, month=month, rule_version=rule_version)
        if not builds:
            raise ValueError(
                f"the Universe for {month:%Y-%m} has no build recorded;"
                " run `tradeline backfill-universe-builds` first"
            )
        (build,) = builds
        if not queries.universe_month_hash(conn, build).matches:
            raise ValueError(
                f"the Universe for {month:%Y-%m} was built from rows that have changed since;"
                " recompute it under a new rule version before freezing"
            )
        months.append(
            MonthSnapshot(
                month=month,
                mode=queries.universe_month_mode(
                    conn, month, rule_version=rule_version, calendar=calendar
                ),
                window_start=build.window_start,
                data_cutoff=build.data_cutoff,
                eod_run_ids=build.eod_run_ids,
                instruments_run_id=build.instruments_run_id,
                eod_hash=build.eod_hash,
                links_hash=build.links_hash,
                cutoff_inferred=build.cutoff_inferred,
            )
        )
        instruments |= members
    return DataSnapshot(
        start=start,
        end=last,
        months=tuple(months),
        adjusted_series={
            i: queries.adjusted_series_hash(conn, i, start=start, end=last)
            for i in sorted(instruments)
        },
    )


def run_members(conn: psycopg.Connection, month: date, rule_version: str) -> set[str]:
    """The month's members as its Runs read them: by Instrument key, or Symbol without one."""
    return {
        m.instrument or m.symbol
        for m in queries.universe_membership(conn, month, as_of=month, rule_version=rule_version)
    }


def _warm_up_start(calendar: TradingCalendar, first: date, days: int) -> date:
    """The trading day ``days`` trading days before ``first``, or the calendar's first day."""
    if days == 0:
        return first
    since = max(calendar.first, first - timedelta(days=2 * days + 14))
    earlier = [d for d in calendar.trading_days(since, first) if d < first]
    return earlier[-days] if len(earlier) >= days else calendar.first
