"""The Holdout: run once per Strategy version, consumed once per Strategy Family (ADR 0007).

A version is its Protocol. Once its Walk-Forward has finished, ``run_holdout``
runs the Protocol's Holdout from a fresh account at its starting Equity, under
the last Window's chosen parameters (the last chosen before it, when the last
Window made no selection, as the Stitched Curve does), its data snapshot
verified first. Positions open on the Holdout's last day stay open: they are
marked at its close, not counted as closed trades. Each day is committed as it
is written, so a run interrupted continues where it stopped.

It runs only once the pre-Holdout checks (``tradeline.holdout_checks``) have
passed at the commit it runs at; until then it is refused, naming each unmet
check, and no attempt is recorded (the checks are, in ``holdout_checks``). Every
other request is recorded in ``holdout_attempts``. A second run by the same
version is refused and the refusal recorded. The family's first run consumes
its Holdout; a later version's run is Not Independent. So is a first version's whose
Protocol says so, with its reason (its family chosen after reading published results
covering the Holdout's years: ``lowvol_v1``), though it consumes it. So is any run after a
data correction on a period an earlier run saw: when a Universe month's or a
member's Adjusted Series hash an earlier Holdout run verified (its Protocol's
snapshot, over that run's Holdout) no longer matches the rows stored now,
whatever family that run was.

A Not Independent run passes the Backtest Gate only together with a Fresh
Period: the trading days after both the consumed Holdout's last day and the
version's freeze (the day it was frozen, in IST, is not one), at least twelve
months long (``fresh_period``, ``standing``). ``run_fresh_period`` runs it: the
parameters the Holdout ran at, in a fresh account at the Protocol's starting
Equity, over the Fresh Period's days to the last with data, recorded
(``fresh_periods``) and extended as more data is collected; the Backtest Gate
scores it with the Holdout's thresholds (``protocol_report``). It is a backtest at
the frozen commit, refused at any other, and refused when the frozen data its
warm-up reads (the Protocol's last months) or the demerger table has changed. A
portfolio Strategy's account starts with an Initial Buy decided on the trading day
before its first, and the Benchmark is bought the day its entrants are; it carries
the Gate once it also holds two Rebalances after it (``gate``).

A Machinery Correction (ADR 0007 amendment) runs no Holdout of its own: the
Holdout run of the Protocol it corrects stands as recorded (``holdout_run_of``),
a request under the correction is refused and recorded, and its standing and
Fresh Period are that version's, from the same first day. Its Fresh Period
account is its own, rerun from that day at the fixed commit and continued.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.dates import IST
from tradeline.engine import SeriesCache, backtest_span, require_fresh_period
from tradeline.freeze import run_members, verify_snapshot, warm_up_start
from tradeline.holdout_checks import require_passed
from tradeline.paper_broker import FILL_MODEL, FillModel
from tradeline.protocol import months_between
from tradeline.risk import RISK_ENGINE, RiskEngine
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar
from tradeline.walk_forward import StrategyFactory, one_cell

log = logging.getLogger(__name__)


class HoldoutRefused(ValueError):
    """A second Holdout run by a version that has run it; the refusal is recorded."""


@dataclass(frozen=True)
class HoldoutResult:
    attempt: queries.HoldoutAttempt
    # Days run by this call (not those an earlier, interrupted call wrote).
    runs: int
    seconds: float

    @property
    def backtest_id(self) -> uuid.UUID:
        assert self.attempt.backtest_id is not None
        return self.attempt.backtest_id

    def summary(self) -> dict[str, Any]:
        attempt = self.attempt
        return {
            "protocol": attempt.protocol,
            "family": attempt.family,
            "backtest_id": str(self.backtest_id),
            "parameters": attempt.parameters,
            "consumes": attempt.consumes,
            "independent": attempt.independent,
            "corrected_since": [str(a) for a in attempt.corrected_since],
            "runs": self.runs,
            "seconds": round(self.seconds, 1),
        }


def run_holdout(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    *,
    protocol: str | None,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> HoldoutResult:
    """Run the Holdout of the frozen Protocol ``protocol`` (its hash, or 12 or more
    characters of it), or the rest of a run an interruption stopped; refuse, and record the
    refusal, if the version has run it whole."""
    began = time.monotonic()
    if not protocol:
        raise ValueError(
            "the Holdout runs under a frozen Protocol and names none;"
            " name the one whose Walk-Forward has finished"
        )
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(
            f"no Protocol {protocol} is frozen; `tradeline protocol freeze` freezes one"
        )
    terms, short = frozen.protocol, frozen.hash[:12]
    if terms.correction is not None:
        reason = (
            f"Protocol {short} is a Machinery Correction of Protocol"
            f" {terms.correction.corrects[:12]}, whose Holdout run stands as recorded;"
            " a correction runs no Holdout of its own"
        )
        _record(conn, frozen, outcome="refused", reason=reason)
        raise HoldoutRefused(reason)
    span = terms.holdout
    parameters = _last_parameters(conn, frozen)
    days = list(calendar.trading_days(span.first, span.last))
    attempt = holdout_run(conn, frozen.hash)
    start = span.first
    if attempt is not None:
        assert attempt.backtest_id is not None
        runs = queries.runs(conn, mode="backtest", backtest_id=attempt.backtest_id)
        if runs and runs[-1].as_of >= span.last:
            reason = (
                f"Protocol {short} already ran the Holdout in account"
                f" {attempt.backtest_id} on {attempt.attempted_at:%Y-%m-%d}; a version runs"
                " it once, and running it again needs a new version"
            )
            _record(conn, frozen, outcome="refused", reason=reason)
            raise HoldoutRefused(reason)
        if runs:
            start = next(d for d in days if d > runs[-1].as_of)
    require_passed(conn, frozen)
    verify_snapshot(conn, frozen, start=span.first, end=span.last)
    if attempt is None:
        attempt = _record(
            conn,
            frozen,
            outcome="ran",
            backtest_id=uuid.uuid4(),
            parameters=parameters,
            corrected_since=_corrected_since(conn, frozen),
        )
    assert attempt.backtest_id is not None
    results = backtest_span(
        conn,
        strategy({a: Decimal(v) for a, v in parameters.items()}),
        frozen,
        start=start,
        end=span.last,
        calendar=calendar,
        tick_sizes=tick_sizes,
        backtest_id=attempt.backtest_id,
        cache=SeriesCache(conn, start=start, end=span.last),
        risk_engine=risk_engine,
        fill_model=fill_model,
    )
    result = HoldoutResult(attempt, len(results), time.monotonic() - began)
    log.info("holdout finished", extra=result.summary())
    return result


def rerun_holdout(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    frozen: queries.FrozenProtocol,
    *,
    backtest_id: uuid.UUID,
    impact: Decimal,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> None:
    """The version's Holdout run written again in the account ``backtest_id`` with the
    Slippage impact c at ``impact``, at the parameters it ran at, from a fresh account:
    the report's c sensitivity, information and never a Holdout run, so it is not an
    attempt. Resumed from the account's last Run. The caller verifies the data snapshot."""
    attempt = holdout_run(conn, frozen.hash)
    if attempt is None or attempt.parameters is None:
        raise ValueError(f"Protocol {frozen.hash[:12]} has not run the Holdout")
    span = frozen.protocol.holdout
    start = span.first
    runs = queries.runs(conn, mode="backtest", backtest_id=backtest_id)
    if runs:
        rest = [d for d in calendar.trading_days(span.first, span.last) if d > runs[-1].as_of]
        if not rest:
            return
        start = rest[0]
    backtest_span(
        conn,
        strategy({a: Decimal(v) for a, v in attempt.parameters.items()}),
        frozen,
        start=start,
        end=span.last,
        calendar=calendar,
        tick_sizes=tick_sizes,
        backtest_id=backtest_id,
        cache=SeriesCache(conn, start=start, end=span.last),
        risk_engine=risk_engine,
        fill_model=fill_model,
        impact=impact,
    )


def _last_parameters(conn: psycopg.Connection, frozen: queries.FrozenProtocol) -> dict[str, str]:
    """The parameters the Stitched Curve ended under: the last Window's selection, or the
    last before it, or a one-cell grid's cell; refused before the Walk-Forward has finished
    or if no Window chose."""
    short, terms = frozen.hash[:12], frozen.protocol
    windows = queries.walk_forward_windows(conn, frozen.hash)
    if len(windows) < len(terms.windows):
        raise ValueError(
            f"the Walk-Forward under Protocol {short} has finished {len(windows)} of"
            f" {len(terms.windows)} Windows; finish it (`tradeline backtest --protocol"
            f" {short}`) before the Holdout"
        )
    only = one_cell(terms.grid)
    if only is not None:
        # One cell: nothing was selected, and the Stitched Curve ran at it.
        return {name: str(value) for name, value in only.items()}
    chosen = [w.parameters for w in windows if w.parameters is not None]
    if not chosen:
        raise ValueError(
            f"no Window of Protocol {short}'s Walk-Forward chose parameters, so there is no"
            " version to run on the Holdout"
        )
    return chosen[-1]


@dataclass(frozen=True)
class FreshPeriod:
    """The trading days after both the consumed Holdout's last day and a version's freeze:
    from ``first`` (None when the Trading Calendar has none yet) to ``last``, the last with
    data stored (None when there is none). It carries the Backtest Gate once it runs
    twelve months, to ``twelve_months_to``: ``long_enough`` when the data reaches the last
    trading day on or before it."""

    first: date | None
    last: date | None
    trading_days: int
    twelve_months_to: date | None
    long_enough: bool


def fresh_period(
    calendar: TradingCalendar, *, holdout_last: date, frozen_on: date, data_last: date | None
) -> FreshPeriod:
    """The Fresh Period of a version frozen on ``frozen_on`` (a day it may have seen, so not
    one of it) after a Holdout ending ``holdout_last``, with data stored to ``data_last``."""
    after = max(holdout_last, frozen_on)
    days = []
    if after < calendar.last:
        days = [d for d in calendar.trading_days(after, calendar.last) if d > after]
    if not days:
        return FreshPeriod(None, None, 0, None, False)
    first = days[0]
    twelve_months_to = _year_after(first) - timedelta(days=1)
    held = [d for d in days if data_last is not None and d <= data_last]
    long_enough = False
    if twelve_months_to <= calendar.last and held:
        due = [d for d in days if d <= twelve_months_to]
        long_enough = held[-1] >= due[-1]
    return FreshPeriod(first, held[-1] if held else None, len(held), twelve_months_to, long_enough)


def _year_after(day: date) -> date:
    """The same day a year on (1 March for 29 February)."""
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return date(day.year + 1, 3, 1)


@dataclass(frozen=True)
class HoldoutStanding:
    """Whether a version's Holdout run may pass the Backtest Gate, before its thresholds are
    scored: an independent run may on the Holdout; a Not Independent run only together with
    a Fresh Period of at least twelve months (``fresh_period``, None for an independent
    run). ``reason`` says which."""

    attempt: queries.HoldoutAttempt
    fresh_period: FreshPeriod | None
    may_pass: bool
    reason: str


def standing(
    conn: psycopg.Connection, protocol: str, *, calendar: TradingCalendar
) -> HoldoutStanding:
    """The standing of the Holdout run by the version under the Protocol whose full hash is
    ``protocol`` (for a Machinery Correction, the run of the Protocol it corrects, and that
    version's Fresh Period); refused if it has not run the Holdout."""
    attempt = holdout_run_of(conn, protocol)
    if attempt is None:
        raise ValueError(f"Protocol {protocol[:12]} has not run the Holdout")
    protocol = attempt.protocol
    if attempt.independent:
        return HoldoutStanding(
            attempt, None, True, "independent: the family's Holdout, consumed by this run"
        )
    lineage = queries.lineage(conn, protocol)
    consumer = queries.protocol(conn, lineage.family.consumed_by or protocol)
    assert consumer is not None
    stored = queries.end_of_day_dates(conn, start=calendar.first, end=calendar.last)
    period = fresh_period(
        calendar,
        holdout_last=consumer.protocol.holdout.last,
        frozen_on=lineage.frozen_at.astimezone(IST).date(),
        data_last=stored[-1] if stored else None,
    )
    why = []
    if attempt.not_independent is not None:
        why.append(attempt.not_independent)
    if not attempt.consumes:
        why.append(f"the family's Holdout was consumed by Protocol {consumer.hash[:12]}")
    if attempt.corrected_since:
        why.append("data an earlier Holdout run saw has been corrected since")
    reason = "Not Independent (" + "; ".join(why) + "): "
    if period.long_enough:
        reason += f"passes only together with its Fresh Period from {period.first}"
    else:
        reason += (
            f"its Fresh Period has {period.trading_days} trading days of data, short of"
            f" twelve months (to {period.twelve_months_to}), so it cannot pass"
        )
    return HoldoutStanding(attempt, period, period.long_enough, reason)


@dataclass(frozen=True)
class FreshPeriodResult:
    run: queries.FreshPeriodRun
    period: FreshPeriod
    # Days run by this call (not those an earlier call wrote).
    runs: int
    seconds: float

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": self.run.protocol,
            "backtest_id": str(self.run.backtest_id),
            "parameters": self.run.parameters,
            "first": self.run.first.isoformat(),
            "last": None if self.period.last is None else self.period.last.isoformat(),
            "long_enough": self.period.long_enough,
            "runs": self.runs,
            "seconds": round(self.seconds, 1),
        }


def run_fresh_period(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    *,
    protocol: str | None,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> FreshPeriodResult:
    """Run the Fresh Period of the version under the frozen Protocol ``protocol`` (its hash,
    or 12 or more characters of it), whose Holdout run is Not Independent, from where its
    account stopped to the last day with data; refused for an independent run, before the
    Holdout has run to its last day, and before the Fresh Period has a day of data."""
    began = time.monotonic()
    if not protocol:
        raise ValueError(
            "a Fresh Period runs under a frozen Protocol and names none;"
            " name the version whose Holdout run is Not Independent"
        )
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(
            f"no Protocol {protocol} is frozen; `tradeline protocol freeze` freezes one"
        )
    short, holdout = frozen.hash[:12], frozen.protocol.holdout
    attempt = holdout_run_of(conn, frozen.hash)
    ran = (
        []
        if attempt is None
        else queries.runs(conn, mode="backtest", backtest_id=attempt.backtest_id)
    )
    if attempt is None or not ran or ran[-1].as_of < holdout.last:
        raise ValueError(
            f"Protocol {short} has not run the Holdout to its last day; run it"
            f" (`tradeline backtest holdout --protocol {short}`) first"
        )
    if attempt.independent:
        raise ValueError(
            f"Protocol {short}'s Holdout run is independent: it is scored on the Holdout and"
            " needs no Fresh Period"
        )
    assert attempt.parameters is not None
    period = standing(conn, frozen.hash, calendar=calendar).fresh_period
    assert period is not None
    if period.first is None or period.last is None:
        raise ValueError(
            f"Protocol {short}'s Fresh Period has no trading day of data yet"
            f" (it starts after {holdout.last} and after the version's freeze)"
        )
    parameters = {a: Decimal(v) for a, v in attempt.parameters.items()}
    require_fresh_period(conn, strategy(parameters), frozen, start=period.first, end=period.last)
    _verify_read_snapshot(conn, frozen, period.first, calendar)
    found = queries.fresh_period_run(conn, frozen.hash)
    if found is None:
        with conn.transaction():
            conn.execute(
                "INSERT INTO fresh_periods (protocol, backtest_id, first, parameters)"
                " VALUES (%s, %s, %s, %s)",
                (
                    frozen.hash,
                    uuid.uuid4(),
                    period.first,
                    json.dumps(attempt.parameters, sort_keys=True),
                ),
            )
        conn.commit()
        found = queries.fresh_period_run(conn, frozen.hash)
        assert found is not None
    written = queries.runs(conn, mode="backtest", backtest_id=found.backtest_id)
    days = [
        d
        for d in calendar.trading_days(found.first, period.last)
        if not written or d > written[-1].as_of
    ]
    results = []
    if days:
        results = backtest_span(
            conn,
            strategy({a: Decimal(v) for a, v in found.parameters.items()}),
            frozen,
            start=days[0],
            end=days[-1],
            calendar=calendar,
            tick_sizes=tick_sizes,
            backtest_id=found.backtest_id,
            cache=SeriesCache(conn, start=days[0], end=days[-1]),
            risk_engine=risk_engine,
            fill_model=fill_model,
            fresh_period=True,
        )
    result = FreshPeriodResult(found, period, len(results), time.monotonic() - began)
    log.info("fresh period finished", extra=result.summary())
    return result


def _verify_read_snapshot(
    conn: psycopg.Connection, frozen: queries.FrozenProtocol, first: date, calendar: TradingCalendar
) -> None:
    """Refuse a Fresh Period from ``first`` if the frozen data it reads is not the rows
    stored now: the Universe months and Adjusted Series of its warm-up's bars, which lie in
    the Protocol's range (its own days lie after it, and the Protocol cannot cover them),
    and the demerger table it credits."""
    terms = frozen.protocol
    start = max(terms.first, warm_up_start(calendar, first, terms.warm_up_trading_days))
    verify_snapshot(conn, frozen, start=start, end=terms.holdout.last)


class _Hashes:
    """The Universe window and Adjusted Series hashes of the rows stored now, each computed
    once, starting from those a Protocol's snapshot was just verified to match."""

    def __init__(self, conn: psycopg.Connection, frozen: queries.FrozenProtocol) -> None:
        self._conn = conn
        terms = frozen.protocol
        snapshot = terms.snapshot
        months = set(months_between(terms.holdout.first, terms.holdout.last))
        self._windows = {
            (m.window_start, m.data_cutoff): (m.eod_hash, m.links_hash)
            for m in snapshot.months
            if m.month in months
        }
        self._series = {
            (i, snapshot.start, snapshot.end): snapshot.adjusted_series[i]
            for month in months
            for i in run_members(conn, month, terms.rule_version)
        }

    def window(self, start: date, cutoff: date) -> tuple[str, str]:
        key = (start, cutoff)
        if key not in self._windows:
            self._windows[key] = queries.universe_window_hashes(
                self._conn, start=start, cutoff=cutoff
            )
        return self._windows[key]

    def series(self, instrument: str, start: date, end: date) -> str:
        key = (instrument, start, end)
        if key not in self._series:
            self._series[key] = queries.adjusted_series_hash(
                self._conn, instrument, start=start, end=end
            )
        return self._series[key]


def _corrected_since(
    conn: psycopg.Connection, frozen: queries.FrozenProtocol
) -> tuple[uuid.UUID, ...]:
    """The earlier Holdout runs, of any family, on a period this one sees, whose verified
    data has since been corrected: a Universe month in the overlap of their Holdout and this
    one, or the Adjusted Series of one of its members, that hashes differently now."""
    span = frozen.protocol.holdout
    now = _Hashes(conn, frozen)
    corrected = []
    for attempt in queries.holdout_attempts(conn):
        if attempt.outcome != "ran" or attempt.protocol == frozen.hash:
            continue
        earlier = queries.protocol(conn, attempt.protocol)
        assert earlier is not None
        terms = earlier.protocol
        first, last = max(span.first, terms.holdout.first), min(span.last, terms.holdout.last)
        if first > last:
            continue
        seen = set(months_between(first, last))
        snapshot = terms.snapshot
        differs = any(
            now.window(m.window_start, m.data_cutoff) != (m.eod_hash, m.links_hash)
            for m in snapshot.months
            if m.month in seen
        ) or any(
            now.series(i, snapshot.start, snapshot.end) != snapshot.adjusted_series[i]
            for month in sorted(seen)
            for i in sorted(run_members(conn, month, terms.rule_version))
        )
        if differs:
            corrected.append(attempt.id)
    return tuple(corrected)


def holdout_run_of(conn: psycopg.Connection, protocol: str) -> queries.HoldoutAttempt | None:
    """The Holdout run the Gate reads for the version under the Protocol whose full hash is
    ``protocol``: its own, or a Machinery Correction's original's
    (``queries.corrected_original``)."""
    frozen = queries.protocol(conn, protocol)
    assert frozen is not None
    return holdout_run(conn, queries.corrected_original(conn, frozen).hash)


def holdout_run(conn: psycopg.Connection, protocol: str) -> queries.HoldoutAttempt | None:
    """The version's Holdout run (its ``ran`` attempt), if it has one, finished or not."""
    ran = [a for a in queries.holdout_attempts(conn, protocol=protocol) if a.outcome == "ran"]
    return ran[0] if ran else None


def _record(
    conn: psycopg.Connection,
    frozen: queries.FrozenProtocol,
    *,
    outcome: str,
    backtest_id: uuid.UUID | None = None,
    parameters: Mapping[str, str] | None = None,
    corrected_since: tuple[uuid.UUID, ...] | None = None,
    reason: str | None = None,
) -> queries.HoldoutAttempt:
    """Write the attempt, the family locked so its first run is the one that consumes it."""
    family = frozen.protocol.family
    attempt_id = uuid.uuid4()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"holdout:{family}",))
        consumes = None
        if outcome == "ran":
            consumes = not any(a.consumes for a in queries.holdout_attempts(conn, family=family))
        conn.execute(
            "INSERT INTO holdout_attempts (id, protocol, family, outcome, backtest_id,"
            " parameters, consumes, corrected_since, reason, not_independent)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                attempt_id,
                frozen.hash,
                family,
                outcome,
                backtest_id,
                None if parameters is None else json.dumps(dict(parameters), sort_keys=True),
                consumes,
                None if corrected_since is None else list(corrected_since),
                reason,
                frozen.protocol.not_independent if outcome == "ran" else None,
            ),
        )
    conn.commit()
    (written,) = (
        a for a in queries.holdout_attempts(conn, protocol=frozen.hash) if a.id == attempt_id
    )
    return written
