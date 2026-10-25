"""Rights issues held through: reported, never modelled (ADR 0020).

A rights issue lets a holder buy new shares at a price under the market's, so
the price falls on its Ex-Date, and the backtest credits nothing for the
entitlement. ``rights_held`` lists each Position of an account held at the
close before a rights issue's Ex-Date (``queries.held_at_close``), with the
Ex-Date's gap in the Raw Series: the Ex-Date's open against the close of the
trading day before. If they matter, the planned fix is a table like the
demerger table (``tradeline.demergers``), valuing entitlements at their NSE
traded price.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg

from tradeline import queries
from tradeline.queries import EngineMode
from tradeline.trading_calendar import TradingCalendar


@dataclass(frozen=True)
class RightsHeld:
    """A Position held at the close before a rights issue's Ex-Date, and the Ex-Date's gap:
    ``open`` over ``previous_close``, less one; None where either bar is missing."""

    position_id: uuid.UUID
    instrument: str
    symbol: str
    ex_date: date
    subject: str
    shares: int
    previous_close: Decimal | None
    open: Decimal | None

    @property
    def gap(self) -> Decimal | None:
        if self.open is None or self.previous_close is None:
            return None
        return (self.open - self.previous_close) / self.previous_close


def rights_held(
    conn: psycopg.Connection,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool = False,
    calendar: TradingCalendar,
) -> list[RightsHeld]:
    """Each of an account's Positions held at the close before a rights issue's Ex-Date, in
    Ex-Date order, with the Ex-Date's gap."""
    events = queries.position_events(conn, mode=mode, backtest_id=backtest_id, dry_run=dry_run)
    if not events:
        return []
    first, last = events[0].day, events[-1].day
    found: list[RightsHeld] = []
    for instrument in sorted({e.instrument for e in events}):
        actions = queries.corporate_actions(
            conn, instrument, start=first + timedelta(days=1), end=last, as_of=last
        )
        seen: set[tuple[date, str]] = set()
        for action in actions:
            if action.kind != "rights" or (action.ex_date, action.subject) in seen:
                continue
            seen.add((action.ex_date, action.subject))
            before = _previous_trading_day(calendar, action.ex_date)
            held = queries.held_at_close(events, instrument, before)
            if not held:
                continue
            previous_close, open_ = _gap(conn, instrument, before, action.ex_date)
            found += [
                RightsHeld(
                    h.position_id,
                    h.instrument,
                    h.symbol,
                    action.ex_date,
                    action.subject,
                    h.shares,
                    previous_close,
                    open_,
                )
                for h in held
            ]
    return sorted(found, key=lambda r: (r.ex_date, r.symbol, str(r.position_id)))


def _previous_trading_day(calendar: TradingCalendar, day: date) -> date:
    days = [d for d in calendar.trading_days(day - timedelta(days=15), day) if d < day]
    if not days:
        raise ValueError(f"no trading day in the fortnight before {day}")
    return days[-1]


def _gap(
    conn: psycopg.Connection, instrument: str, before: date, ex_date: date
) -> tuple[Decimal | None, Decimal | None]:
    rows = queries.end_of_day(conn, instrument, start=before, end=ex_date, as_of=ex_date)
    closes = [r.close for r in rows if r.trade_date == before]
    opens = [r.open for r in rows if r.trade_date == ex_date]
    return (closes[0] if closes else None), (opens[0] if opens else None)
