"""The Drift Replay: a frozen Protocol's runs again at a later commit (ADR 0007, 2026-10-25).

Every run under a Protocol is refused at a commit other than the one it was frozen
at, but one: ``replay_drift`` reruns its Stitched Curve and any Fresh Period days,
each in an account of its own, at the commit this process runs, and compares
holdings, Fills, costs, dividends, Demerger Credits and the tax ledger with the
runs at the frozen commit. The Stitched Curve runs as the c sensitivity's does
(``walk_forward.restitch``): each Window's Out-of-Sample days at the parameters it
chose, nothing selected again; the Fresh Period from its first day to the frozen
account's last, at the parameters its Holdout ran at.

The comparison (``differences``, pure) leaves out what can differ without the
engine behaving differently: ids, timestamps and formatting (a decimal compares by
value, so ``1.0`` equals ``1.00``). Anything else that differs is listed, by
curve, kind, the row it is in and its field, with the frozen and the replayed
value; ``money`` says whether the field is an amount or a count of shares rather
than a label. A replay with no difference passes. One with any fails: the change
is reverted, or a new version is frozen (a Machinery Correction or not).

Every request is recorded in ``drift_replays``. One naming the Holdout is refused
and recorded, and runs nothing: the engine itself refuses a Drift Replay's range
that touches the Holdout (``engine.backtest_span``). A request at a commit already
replayed reads the recorded result instead of running again, unless the Fresh
Period has run further since. A Drift Replay's accounts are named by nothing a
Gate reads, and it is never a Window run. A paper Run (Phase 3) refuses to start at
a commit other than the frozen one without a pass (``queries.drift_replay_passed``).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

import psycopg

from tradeline import queries, revision
from tradeline.engine import SeriesCache, backtest_span
from tradeline.freeze import verify_snapshot
from tradeline.paper_broker import FILL_MODEL, FillModel
from tradeline.risk import RISK_ENGINE, RiskEngine
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar
from tradeline.walk_forward import StrategyFactory, restitch

log = logging.getLogger(__name__)

# The curves a Drift Replay reruns, in order; the Holdout is never one.
CURVES = ("stitched_curve", "fresh_period")
HOLDOUT = "holdout"
# Fields that label a row rather than state an amount or a count of shares.
_LABELS = frozenset({"exit_reason", "reason", "subject", "step", "settled", "present"})


class DriftReplayRefused(ValueError):
    """A Drift Replay naming the Holdout; the refusal is recorded."""


# One account as the comparison reads it: by kind, each row by its key, its fields by name.
AccountRecord = dict[str, dict[str, dict[str, Any]]]


def account_record(conn: psycopg.Connection, backtest_id: uuid.UUID) -> AccountRecord:
    """The backtest account ``backtest_id`` as the comparison reads it: every Run's cash and
    Equity, every Position, Fill and its costs, dividend, Demerger Credit, tax debit and
    day's Liquidation Value, keyed by what they are rather than by their ids."""
    account: dict[str, Any] = {"mode": "backtest", "backtest_id": backtest_id}
    record: AccountRecord = {
        kind: {}
        for kind in (
            "account",
            "holdings",
            "fills",
            "costs",
            "dividends",
            "demerger_credits",
            "tax",
            "liquidation",
        )
    }
    for run in queries.runs(conn, **account):
        _add(
            record["account"],
            f"{run.as_of} {run.portfolio_step or 'run'}",
            {"cash": run.cash, "equity": run.equity},
        )
    for position in queries.positions(conn, **account):
        _add(
            record["holdings"],
            f"{position.symbol} opened {position.opened_on}",
            {
                "shares": position.shares,
                "entry": position.entry,
                "stop": position.stop,
                "target": position.target,
                "time_stop_days": position.time_stop_days,
                "closed_on": position.closed_on,
                "exit_reason": position.exit_reason,
                "exit_price": position.exit_price,
            },
        )
    for fill in queries.fills(conn, **account):
        key = _add(
            record["fills"],
            f"{fill.day} {fill.symbol} {fill.side}",
            {"price": fill.price, "shares": fill.shares},
        )
        charges = {} if fill.charges is None else fill.charges.items()
        record["costs"][key] = dict(charges)
    for dividend in queries.dividend_credits(conn, **account):
        _add(
            record["dividends"],
            f"{dividend.ex_date} {dividend.symbol} {dividend.subject}",
            {
                "day": dividend.day,
                "shares": dividend.shares,
                "per_share": dividend.per_share,
                "cash": dividend.cash,
                "reason": dividend.reason,
            },
        )
    for credit in queries.demerger_credits(conn, **account):
        _add(
            record["demerger_credits"],
            f"{credit.day} {credit.parent} {credit.child}",
            {
                "shares": credit.shares,
                "ratio": credit.ratio,
                "close": credit.close,
                "cash": credit.cash,
                "basis": credit.basis,
            },
        )
    for debit in queries.tax_debits(conn, **account):
        _add(
            record["tax"],
            f"FY{debit.financial_year}{' settled' if debit.settled else ''}",
            {
                f.name: getattr(debit, f.name)
                for f in fields(debit)
                if f.name not in ("financial_year", "settled")
            },
        )
    for day in queries.liquidation_values(conn, **account):
        benchmark = day.benchmark
        _add(
            record["liquidation"],
            str(day.day),
            {
                "value": day.value,
                "tax_due": day.tax_due,
                "dropped_short_term_loss": day.dropped_short_term_loss,
                "dropped_long_term_loss": day.dropped_long_term_loss,
                "benchmark_value": None if benchmark is None else benchmark.value,
                "benchmark_tax_due": None if benchmark is None else benchmark.tax_due,
            },
        )
    return record


def _add(rows: dict[str, dict[str, Any]], key: str, row: dict[str, Any]) -> str:
    """Add ``row`` under ``key``, or under ``key`` numbered when it is taken; the key used."""
    found, n = key, 1
    while found in rows:
        n += 1
        found = f"{key} #{n}"
    rows[found] = row
    return found


def differences(curve: str, frozen: AccountRecord, replay: AccountRecord) -> list[dict[str, Any]]:
    """Every difference between the frozen run's account and the replay's on ``curve``: a
    field whose values differ, or a row only one of them has (field ``present``)."""
    found = []
    for kind in frozen.keys() | replay.keys():
        before, after = frozen.get(kind, {}), replay.get(kind, {})
        for key in sorted(before.keys() | after.keys()):
            if key not in before or key not in after:
                found.append(_difference(curve, kind, key, "present", key in before, key in after))
                continue
            for name in sorted(before[key].keys() | after[key].keys()):
                old, new = before[key].get(name), after[key].get(name)
                if old != new:
                    found.append(_difference(curve, kind, key, name, old, new))
    order = list(frozen) + [k for k in replay if k not in frozen]
    return sorted(found, key=lambda d: (order.index(d["kind"]), d["key"], d["field"]))


def _difference(curve: str, kind: str, key: str, name: str, old: Any, new: Any) -> dict[str, Any]:
    return {
        "curve": curve,
        "kind": kind,
        "key": key,
        "field": name,
        "frozen": _text(old),
        "replay": _text(new),
        "money": name not in _LABELS,
    }


def _text(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal | Fraction | date):
        return str(value)
    raise TypeError(f"cannot record {value!r} in a difference")


@dataclass(frozen=True)
class DriftReplayResult:
    replay: queries.DriftReplay
    # Read from an earlier request at the same commit, nothing run.
    cached: bool
    # Days run by this call.
    runs: int
    seconds: float

    @property
    def passed(self) -> bool:
        return self.replay.outcome == "pass"

    def summary(self) -> dict[str, Any]:
        replay = self.replay
        differences = replay.differences or []
        kinds: dict[str, int] = {}
        for d in differences:
            kinds[f"{d['curve']} {d['kind']}"] = kinds.get(f"{d['curve']} {d['kind']}", 0) + 1
        return {
            "protocol": replay.protocol,
            "git_commit": replay.git_commit,
            "frozen_commit": replay.frozen_commit,
            "curves": list(replay.curves),
            "outcome": replay.outcome,
            "accounts": {c: str(a) for c, a in replay.accounts.items()},
            "fresh_through": None if replay.fresh_through is None else str(replay.fresh_through),
            "differences": len(differences),
            "by_kind": kinds,
            "first_differences": differences[:20],
            "cached": self.cached,
            "runs": self.runs,
            "seconds": round(self.seconds, 1),
        }


def replay_drift(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    *,
    protocol: str | None,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    curves: Sequence[str] | None = None,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> DriftReplayResult:
    """The Drift Replay of the frozen Protocol ``protocol`` (its hash, or 12 or more
    characters of it) at this commit: ``curves`` (by default the Stitched Curve, and the
    Fresh Period once it has run) rerun and compared with the frozen runs, recorded; or the
    result recorded at this commit before. Refused, and recorded, if ``curves`` names the
    Holdout; refused at the frozen commit itself."""
    began = time.monotonic()
    if not protocol:
        raise ValueError("a Drift Replay replays a frozen Protocol and names none; name one")
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(
            f"no Protocol {protocol} is frozen; `tradeline protocol freeze` freezes one"
        )
    terms, short = frozen.protocol, frozen.hash[:12]
    commit = revision.running_commit()
    if commit == terms.git_commit:
        raise ValueError(
            f"Protocol {short} was frozen at this commit ({commit[:12]}): its own runs are the"
            " frozen ones, and a Drift Replay runs at a later commit"
        )
    fresh = queries.fresh_period_run(conn, frozen.hash)
    if curves is None:
        curves = CURVES if fresh is not None else CURVES[:1]
    curves = tuple(curves)
    if HOLDOUT in curves:
        reason = (
            f"a Drift Replay never runs the Holdout: Protocol {short}'s Holdout runs once, at"
            f" its frozen commit {terms.git_commit[:12]}; replay the Stitched Curve and the"
            " Fresh Period"
        )
        _record(conn, frozen, commit, curves, outcome="refused", reason=reason)
        raise DriftReplayRefused(reason)
    unknown = [c for c in curves if c not in CURVES]
    if unknown or not curves or len(set(curves)) < len(curves):
        raise ValueError(f"a Drift Replay replays some of {', '.join(CURVES)}, not {curves}")
    walked = queries.walk_forward(conn, frozen.hash)
    windows = queries.walk_forward_windows(conn, frozen.hash)
    if walked is None or len(windows) < len(terms.windows):
        raise ValueError(
            f"Protocol {short}'s Walk-Forward has not finished; a Drift Replay compares with"
            " the frozen runs, so they run first"
        )
    frozen_accounts = {"stitched_curve": walked.backtest_id}
    fresh_through = None
    if "fresh_period" in curves:
        written = (
            []
            if fresh is None
            else queries.runs(conn, mode="backtest", backtest_id=fresh.backtest_id)
        )
        if fresh is None or not written:
            raise ValueError(
                f"Protocol {short} has run no Fresh Period day; replay the Stitched Curve alone"
            )
        frozen_accounts["fresh_period"] = fresh.backtest_id
        fresh_through = written[-1].as_of
    for earlier in reversed(queries.drift_replays(conn, protocol=frozen.hash, git_commit=commit)):
        if (
            earlier.outcome != "refused"
            and earlier.curves == curves
            and earlier.fresh_through == fresh_through
        ):
            result = DriftReplayResult(earlier, True, 0, time.monotonic() - began)
            log.info("drift replay read from an earlier request", extra=result.summary())
            return result
    replayed = {curve: uuid.uuid4() for curve in curves}
    runs = 0
    if "stitched_curve" in curves:
        verify_snapshot(conn, frozen, start=terms.first, end=terms.windows[-1].out_of_sample.last)
        restitch(
            conn,
            strategy,
            frozen,
            backtest_id=replayed["stitched_curve"],
            impact=None,
            calendar=calendar,
            tick_sizes=tick_sizes,
            risk_engine=risk_engine,
            fill_model=fill_model,
            drift_replay=True,
        )
        runs += len(queries.runs(conn, mode="backtest", backtest_id=replayed["stitched_curve"]))
    if fresh is not None and fresh_through is not None:
        days = list(calendar.trading_days(fresh.first, fresh_through))
        results = backtest_span(
            conn,
            strategy({a: Decimal(v) for a, v in fresh.parameters.items()}),
            frozen,
            start=days[0],
            end=days[-1],
            calendar=calendar,
            tick_sizes=tick_sizes,
            backtest_id=replayed["fresh_period"],
            cache=SeriesCache(conn, start=days[0], end=days[-1]),
            risk_engine=risk_engine,
            fill_model=fill_model,
            fresh_period=True,
            drift_replay=True,
        )
        runs += len(results)
    found = []
    for curve in curves:
        found += differences(
            curve,
            account_record(conn, frozen_accounts[curve]),
            account_record(conn, replayed[curve]),
        )
    replay = _record(
        conn,
        frozen,
        commit,
        curves,
        outcome="fail" if found else "pass",
        accounts=replayed,
        fresh_through=fresh_through,
        differences=found,
    )
    result = DriftReplayResult(replay, False, runs, time.monotonic() - began)
    log.info("drift replay finished", extra=result.summary())
    return result


def _record(
    conn: psycopg.Connection,
    frozen: queries.FrozenProtocol,
    commit: str,
    curves: Sequence[str],
    *,
    outcome: str,
    accounts: Mapping[str, uuid.UUID] | None = None,
    fresh_through: date | None = None,
    differences: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> queries.DriftReplay:
    replay_id, accounts = uuid.uuid4(), accounts or {}
    with conn.transaction():
        conn.execute(
            "INSERT INTO drift_replays (id, protocol, git_commit, frozen_commit, curves,"
            " outcome, stitched_backtest_id, fresh_backtest_id, fresh_through, differences,"
            " reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                replay_id,
                frozen.hash,
                commit,
                frozen.protocol.git_commit,
                list(curves),
                outcome,
                accounts.get("stitched_curve"),
                accounts.get("fresh_period"),
                fresh_through,
                None if differences is None else json.dumps(differences),
                reason,
            ),
        )
    conn.commit()
    (written,) = (r for r in queries.drift_replays(conn, protocol=frozen.hash) if r.id == replay_id)
    return written
