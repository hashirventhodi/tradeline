"""What the full report adds for a portfolio family judged by the Benchmark Gate (#98, ADR 0015
amendment of 2026-10-25), read from the Ledger.

For each curve (an account read by ``tradeline.report.read_account``):

- ``against_benchmark``: its Liquidation Values against the Benchmark's after tax, the
  Gate's reading (``tradeline.metrics.benchmark_comparison``), and before tax: the
  Strategy's Liquidation Value with the tax it owes and the tax debited on earlier days
  added back, the Benchmark's with the tax on its gain added back. Each with the
  Strategy's Sharpe on that curve at the Protocol's risk-free rate. Before tax is an
  add-back to the run that paid tax, not a run without it (``BEFORE_TAX_SAYS``): the cash
  a debit took was not invested after it, so its growth is not in the figure.
- ``benchmark_curve``: the curve as the Benchmark Gate reads it, with its Rebalances
  counted as the Protocol's Gate says: the Runs that decided a Rebalance on a day
  before the curve's last (``REBALANCES_COUNTED``).
- ``factor_fund_json``: the factor fund at its NAV (as of each day, chained across its
  frozen unit splits) bought for the starting Equity on the curve's first day, with no
  charge and no tax, against the Strategy's Liquidation Value before tax. Reported, never
  gated.
- ``portfolio_json``: at the Initial Buy and every Rebalance, the cash and each sector's
  weight of Equity on the first day the entrants are held, with each sector's peak
  weight from then until the next one's; the largest weight any name reached; the
  dividends credited, parsed and not; every Demerger Credit and every rights issue a
  Position was held through, each with its effect on excess return as a fraction of
  the starting Equity (a credit's cash; a rights issue's Ex-Date gap times the shares,
  the fall no entitlement is credited for). Each Initial Buy and Rebalance lists its
  buys the open did not fill: an unfilled Rebalance buy is never sent again, so its
  share stays in cash until the next Rebalance (``docs/risk-controls.md``).

Weights are of the day's Equity: cash after the day's session plus each held Position at
the day's close as it stood that day (its Adjusted Series as of the day the Position
was last held, times the factors of the restatements since). A Position is held at a
day's close from the day it opened to the day before it closed; its sector is the one
its Pass recorded.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import psycopg

from tradeline import queries
from tradeline.benchmark import chained_navs, navs
from tradeline.gate import BenchmarkCurve, benchmark_curve
from tradeline.metrics import Comparison, benchmark_comparison, sharpe
from tradeline.protocol import Protocol, fixed_decimal
from tradeline.report import FRACTION, RATIO, RUPEE, Account, rounded
from tradeline.rights import rights_held
from tradeline.risk import UNCLASSIFIED
from tradeline.trading_calendar import TradingCalendar

# How far before an account's first day the factor fund's NAV is looked for.
NAV_LOOKBACK = timedelta(days=31)
SURCHARGE = (
    "the tax ledger leaves out the surcharge on high incomes, so its tax is optimistic for an"
    " Operator who pays one; the Rs 1.25 lakh long-term exemption is left out on both sides"
)
BEFORE_TAX_SAYS = (
    "the run that paid tax with the tax debited before the day and the tax owed added back"
    " to the Strategy, and the tax on its gain to the Benchmark; an add-back, not a backtest"
    " run without tax: the cash a debit took is not grown after it"
)
FACTOR_FUND_SAYS = (
    "the factor fund at its AMFI NAV, chained across its unit splits, bought for the"
    " starting Equity on the curve's first day with no charge and no tax, against the"
    " Strategy's Liquidation Value before tax; reported, never gated"
)


@dataclass(frozen=True)
class Series:
    """A curve's daily values: the Strategy's Liquidation Value and the Benchmark's, after
    tax and before."""

    days: tuple[date, ...]
    after: tuple[Decimal, ...]
    benchmark_after: tuple[Decimal, ...]
    before: tuple[Decimal, ...]
    benchmark_before: tuple[Decimal, ...]


def series(account: Account) -> Series:
    """The account's Liquidation Values on each of its days, as the engine recorded them."""
    by_day = {d.day: d for d in account.liquidation}
    after, benchmark_after, before, benchmark_before = [], [], [], []
    for day in account.days:
        found = by_day.get(day)
        if found is None or found.benchmark is None:
            raise ValueError(f"the account records no Liquidation Value and Benchmark on {day}")
        paid = sum((t.tax for t in account.tax_debits if t.day < day), Decimal(0))
        after.append(found.value)
        before.append(found.value + found.tax_due + paid)
        benchmark_after.append(found.benchmark.value)
        benchmark_before.append(found.benchmark.value + found.benchmark.tax_due)
    return Series(
        account.days, tuple(after), tuple(benchmark_after), tuple(before), tuple(benchmark_before)
    )


def rebalances(account: Account) -> int:
    """The Rebalances the account decided on a day before its last (``REBALANCES_COUNTED``)."""
    last = account.days[-1]
    return sum(r.portfolio_step == "rebalance" and r.as_of < last for r in account.runs)


def benchmark_gate_curve(account: Account, terms: Protocol) -> BenchmarkCurve:
    found = series(account)
    comparison = benchmark_comparison(
        found.after, found.benchmark_after, start=terms.starting_equity
    )
    return benchmark_curve(rebalances=rebalances(account), comparison=comparison)


def against_benchmark(account: Account, terms: Protocol) -> dict[str, Any]:
    found, start = series(account), terms.starting_equity
    rate = terms.risk_free_rate
    return {
        "after_tax": _comparison_json(
            benchmark_comparison(found.after, found.benchmark_after, start=start),
            found.after,
            found.benchmark_after,
            sharpe([start, *found.after], risk_free_rate=rate),
        ),
        "before_tax": {
            "note": BEFORE_TAX_SAYS,
            **_comparison_json(
                benchmark_comparison(found.before, found.benchmark_before, start=start),
                found.before,
                found.benchmark_before,
                sharpe([start, *found.before], risk_free_rate=rate),
            ),
        },
    }


def _comparison_json(
    found: Comparison,
    strategy: Sequence[Decimal],
    benchmark: Sequence[Decimal],
    ratio: Decimal | None,
) -> dict[str, Any]:
    return {
        "strategy_value": rounded(strategy[-1], RUPEE),
        "benchmark_value": rounded(benchmark[-1], RUPEE),
        "strategy_return": rounded(found.strategy_return, FRACTION),
        "benchmark_return": rounded(found.benchmark_return, FRACTION),
        "excess": rounded(found.excess, FRACTION),
        "drawdown": rounded(found.drawdown, FRACTION),
        "benchmark_drawdown": rounded(found.benchmark_drawdown, FRACTION),
        "information_ratio": rounded(found.information_ratio, RATIO),
        "tracking_error": rounded(found.tracking_error, FRACTION),
        "sharpe": rounded(ratio, RATIO),
    }


def factor_fund_json(conn: psycopg.Connection, account: Account, terms: Protocol) -> dict[str, Any]:
    """The factor fund against the Strategy's Liquidation Value before tax."""
    assert terms.benchmark is not None
    code, splits = terms.benchmark.factor_fund, terms.benchmark.factor_fund_splits
    first, last = account.days[0], account.days[-1]
    stored = navs(conn, code, start=first - NAV_LOOKBACK, end=last)
    chained = dict(
        zip(
            (n.nav_date for n in stored),
            chained_navs([(n.nav_date, n.nav) for n in stored], splits),
            strict=True,
        )
    )
    as_of: list[Decimal] = []
    known = sorted(chained)
    index, latest = 0, None
    for day in account.days:
        while index < len(known) and known[index] <= day:
            latest = chained[known[index]]
            index += 1
        if latest is None:
            raise LookupError(f"the factor fund {code} has no NAV on or before {day}")
        as_of.append(latest)
    start = terms.starting_equity
    values = [start * nav / as_of[0] for nav in as_of]
    found = benchmark_comparison(series(account).before, values, start=start)
    return {
        "scheme_code": code,
        "says": FACTOR_FUND_SAYS,
        "unit_splits": [
            {"ex_date": s.ex_date.isoformat(), "ratio": fixed_decimal(s.ratio)}
            for s in splits
            if first < s.ex_date <= last
        ],
        "value": rounded(values[-1], RUPEE),
        "return": rounded(found.benchmark_return, FRACTION),
        "drawdown": rounded(found.benchmark_drawdown, FRACTION),
        "strategy_before_tax_return": rounded(found.strategy_return, FRACTION),
        "excess": rounded(found.excess, FRACTION),
        "information_ratio": rounded(found.information_ratio, RATIO),
        "tracking_error": rounded(found.tracking_error, FRACTION),
    }


@dataclass(frozen=True)
class _Held:
    """A Position's value at each day's close it was held."""

    symbol: str
    sector: str
    values: Mapping[date, Decimal]


def _holdings(conn: psycopg.Connection, account: Account, tags: queries.Account) -> list[_Held]:
    """Each Position's value at the close of every account day it was held."""
    events = queries.position_events(conn, **tags)
    sectors = {(p.run_id, p.rank): p.sector for p in account.passes}
    positions = {p.id: p for p in queries.positions(conn, **tags)}
    by_position: dict[uuid.UUID, list[queries.LedgerPositionEvent]] = {}
    for event in events:
        by_position.setdefault(event.position_id, []).append(event)
    found = []
    for position_id, own in by_position.items():
        position = positions[position_id]
        held_days = [
            d
            for d in account.days
            if own[0].day <= d and (position.closed_on is None or d < position.closed_on)
        ]
        if not held_days:
            continue
        bars = queries.adjusted_daily_candles(
            conn,
            position.instrument,
            start=held_days[0],
            end=held_days[-1],
            as_of=held_days[-1],
        )
        closes = {b.trade_date: b.close for b in bars if b.close is not None}
        values: dict[date, Decimal] = {}
        close: Decimal | None = None
        for day in held_days:
            close = closes.get(day, close)
            if close is None:
                raise ValueError(f"{position.symbol} has no close on or before {day}")
            state = [e for e in own if e.day <= day][-1]
            later = Fraction(1)
            for e in own:
                if e.day > day and e.event == "restated" and e.factor is not None:
                    later *= e.factor
            values[day] = state.shares * close * Decimal(later.numerator) / later.denominator
        found.append(
            _Held(
                position.symbol, sectors.get((position.run_id, position.rank), UNCLASSIFIED), values
            )
        )
    return found


def portfolio_json(
    conn: psycopg.Connection,
    account: Account,
    terms: Protocol,
    tags: queries.Account,
    *,
    calendar: TradingCalendar,
) -> dict[str, Any]:
    held = _holdings(conn, account, tags)
    cash: dict[date, Decimal] = {}
    for run in account.runs:
        if run.portfolio_step == "initial_buy":
            continue
        if run.cash is None:
            raise ValueError(f"the Run on {run.as_of} records no cash; backtest it again")
        cash[run.as_of] = run.cash
    equity = {
        day: cash[day] + sum((h.values.get(day, Decimal(0)) for h in held), Decimal(0))
        for day in account.days
    }
    sector_weights: dict[date, dict[str, Decimal]] = {}
    for day in account.days:
        weights: dict[str, Decimal] = {}
        for h in held:
            if day in h.values:
                weights[h.sector] = weights.get(h.sector, Decimal(0)) + h.values[day] / equity[day]
        sector_weights[day] = weights
    largest: tuple[Decimal, date, str] | None = None
    for day in account.days:
        for h in sorted(held, key=lambda h: h.symbol):
            if day in h.values:
                weight = h.values[day] / equity[day]
                if largest is None or weight > largest[0]:
                    largest = (weight, day, h.symbol)
    orders = queries.orders(conn, **tags)
    return {
        "rebalances": _rebalances(account, cash, equity, sector_weights, orders),
        "largest_name_weight": None
        if largest is None
        else {
            "symbol": largest[2],
            "day": largest[1].isoformat(),
            "weight": rounded(largest[0], FRACTION),
        },
        "dividends": _dividends(conn, tags),
        "demerger_credits": [
            {
                "parent": c.parent,
                "child": c.child,
                "ex_date": c.ex_date.isoformat(),
                "day": c.day.isoformat(),
                "shares": c.shares,
                "cash": rounded(c.cash, RUPEE),
                "effect_on_excess": rounded(c.cash / terms.starting_equity, FRACTION),
            }
            for c in queries.demerger_credits(conn, **tags)
        ],
        "rights_issues": [
            {
                "symbol": r.symbol,
                "ex_date": r.ex_date.isoformat(),
                "subject": r.subject,
                "shares": r.shares,
                "previous_close": rounded(r.previous_close, RUPEE),
                "open": rounded(r.open, RUPEE),
                "gap": rounded(r.gap, FRACTION),
                "effect_on_excess": None
                if r.open is None or r.previous_close is None
                else rounded(
                    r.shares * (r.open - r.previous_close) / terms.starting_equity, FRACTION
                ),
            }
            for r in rights_held(conn, **tags, calendar=calendar)
            if r.ex_date in equity
        ],
    }


def _rebalances(
    account: Account,
    cash: Mapping[date, Decimal],
    equity: Mapping[date, Decimal],
    weights: Mapping[date, Mapping[str, Decimal]],
    orders: Sequence[queries.LedgerOrder],
) -> list[dict[str, Any]]:
    """The Initial Buy and each Rebalance: the day its entrants are first held (the day after
    their buys' decision), its cash and sector weights then, each sector's peak until the
    next one's, and every buy its buys' Run placed that the open did not fill."""
    days = list(account.days)
    found: list[tuple[str, date, date | None, uuid.UUID | None]] = []
    runs = list(account.runs)
    for i, run in enumerate(runs):
        if run.portfolio_step not in ("initial_buy", "rebalance"):
            continue
        buys = (
            run
            if run.portfolio_step == "initial_buy"
            else next((r for r in runs[i + 1 :] if r.portfolio_step == "rebalance_buys"), None)
        )
        held_from = None if buys is None else next((d for d in days if d > buys.as_of), None)
        found.append((run.portfolio_step, run.as_of, held_from, None if buys is None else buys.id))
    entries = []
    for i, (step, decided, held_from, buys_run) in enumerate(found):
        entry: dict[str, Any] = {
            "step": step,
            "decided": decided.isoformat(),
            "held_from": None if held_from is None else held_from.isoformat(),
            "cash": None,
            "sectors": [],
            "peak_until_next": [],
            "unfilled_buys": [
                {
                    "symbol": o.symbol,
                    "session": o.session.isoformat(),
                    "limit": rounded(o.limit_price, RUPEE),
                    "shares": o.shares,
                    "reason": o.cancel_reason,
                }
                for o in orders
                if o.run_id == buys_run and o.purpose == "entry" and o.cancelled is not None
            ],
        }
        if held_from is not None:
            following = [h for _, _, h, _ in found[i + 1 :] if h is not None]
            until = following[0] if following else None
            span = [d for d in days if held_from <= d and (until is None or d < until)]
            entry["cash"] = rounded(cash[held_from] / equity[held_from], FRACTION)
            entry["sectors"] = [
                {"sector": s, "weight": rounded(w, FRACTION)}
                for s, w in sorted(weights[held_from].items())
            ]
            peaks: dict[str, tuple[Decimal, date]] = {}
            for day in span:
                for sector, weight in weights[day].items():
                    if sector not in peaks or weight > peaks[sector][0]:
                        peaks[sector] = (weight, day)
            entry["peak_until_next"] = [
                {"sector": s, "weight": rounded(w, FRACTION), "day": d.isoformat()}
                for s, (w, d) in sorted(peaks.items())
            ]
        entries.append(entry)
    return entries


def _dividends(conn: psycopg.Connection, tags: queries.Account) -> dict[str, Any]:
    credits = queries.dividend_credits(conn, **tags)
    parsed = [c for c in credits if c.cash is not None]
    unparsed = [c for c in credits if c.cash is None]
    return {
        "parsed": len(parsed),
        "unparsed": len(unparsed),
        "credited": rounded(sum((c.cash for c in parsed if c.cash is not None), Decimal(0)), RUPEE),
        "unparsed_subjects": [
            {
                "symbol": c.symbol,
                "ex_date": c.ex_date.isoformat(),
                "subject": c.subject,
                "reason": c.reason,
            }
            for c in unparsed
        ],
    }


def notes_json(terms: Protocol, *, not_independent: str | None) -> dict[str, Any]:
    """What qualifies the result beyond its numbers: the surcharge left out, how the Gate
    counts Rebalances, the Protocol's notes, and the Holdout run's Not Independent reason
    when it is one."""
    gate = terms.gate
    return {
        "surcharge": SURCHARGE,
        "rebalances_counted": getattr(gate, "rebalances_counted", None),
        **dict(sorted(terms.notes.items())),
        "not_independent": not_independent,
    }
