"""The full report: a Protocol's Backtest Gate and everything that qualifies it, read from the
Ledger.

``report_protocol`` runs once a Protocol's Walk-Forward has finished and its
Holdout run has reached the Holdout's last day. It first writes what the report
needs that is not yet in the Ledger: the c sensitivity, the Stitched Curve and
the Holdout run again, each in its own account, with the Slippage impact c at
zero and at twice the Protocol's (``walk_forward.restitch``,
``holdout.rerun_holdout``), at the parameters each Window chose and the Holdout
ran at, so they differ in costs alone and are never read as a Protocol result.
Each is recorded (``cost_sensitivities``) before it runs and resumes where an
interruption stopped it. Then it evaluates the Backtest Gate (``tradeline.gate``)
on the two curves, with the Holdout run's standing (``holdout.standing``: a Not
Independent run cannot pass without a Fresh Period), and records the evaluation
with its inputs and the Protocol's thresholds (``gate_evaluations``), every
time it is asked.

``read_protocol_report`` is the report: a reading of the Ledger and nothing
else, so rendered twice from one Ledger it reads the same. Its JSON states the
Gate outcome as last recorded and the days a daily-loss or drawdown Halt would
have fired on either curve (at the Protocol's Halt fractions) first, then every
Window's selection and out-of-sample result, the Stitched Curve's and the
Holdout's metrics, their Gapped Stops with the realised loss, where the Limits
bound (Candidates beyond the Passes, the Participation Cap, Unclassified), the c
sensitivity, every Universe month's mode, the family lineage and the
contract-note status. It holds no backtest id and no timing, so two runs of one
Protocol over one data read the same. The Markdown, for posting on the Phase 2
issue, renders it in that order and adds the runtime: the time between the
Ledger's first and last write for each stage, which includes any pause.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.dates import IST
from tradeline.freeze import verify_snapshot
from tradeline.gate import CurveMetrics, GateOutcome, evaluate
from tradeline.holdout import holdout_run, rerun_holdout, standing
from tradeline.metrics import HaltDay
from tradeline.paper_broker import FILL_MODEL, FillModel
from tradeline.report import (
    FRACTION,
    RATIO,
    Account,
    Scores,
    halt_days_json,
    read_account,
    rounded,
)
from tradeline.risk import RISK_ENGINE, RiskEngine
from tradeline.tick_sizes import TickSizes
from tradeline.trading_calendar import TradingCalendar
from tradeline.walk_forward import StrategyFactory, restitch, window_json

log = logging.getLogger(__name__)

CURVES = ("stitched_curve", "holdout")
CURVE_NAMES = {"stitched_curve": "Stitched Curve", "holdout": "Holdout"}


@dataclass(frozen=True)
class Stage:
    """A stage's runtime as the Ledger dates it: its first write to its last."""

    name: str
    started: datetime
    finished: datetime

    @property
    def seconds(self) -> float:
        return (self.finished - self.started).total_seconds()


@dataclass(frozen=True)
class ProtocolReport:
    frozen: queries.FrozenProtocol
    windows: tuple[queries.WalkForwardWindow, ...]
    stitched: Account
    attempt: queries.HoldoutAttempt
    holdout: Account
    evaluation: queries.GateEvaluation
    # The c sensitivity accounts by impact: each curve's scores.
    sensitivity: tuple[tuple[Decimal, dict[str, Scores]], ...]
    lineage: queries.Lineage
    stages: tuple[Stage, ...]

    def _accounts(self) -> dict[str, Account]:
        return {"stitched_curve": self.stitched, "holdout": self.holdout}

    def _halts(self) -> dict[str, list[HaltDay]]:
        halts = self.frozen.protocol.halts
        return {curve: account.halt_days(halts) for curve, account in self._accounts().items()}

    def as_json(self) -> dict[str, Any]:
        frozen, terms, evaluation = self.frozen, self.frozen.protocol, self.evaluation
        rate = terms.risk_free_rate
        accounts = self._accounts()
        halts = self._halts()
        return {
            "protocol": frozen.hash,
            "strategy": {
                "family": terms.family,
                "name": terms.name,
                "code_revision": terms.code_revision,
            },
            "gate": {
                "outcome": evaluation.outcome,
                "reason": evaluation.reason,
                "thresholds": {
                    name: evaluation.thresholds[name]
                    for name in (
                        "sharpe_above",
                        "drawdown_below",
                        "profit_factor_above",
                        "min_stitched_trades",
                    )
                },
                "checks": [_check_json(c) for c in evaluation.checks],
                "holdout_trades": evaluation.inputs["holdout"]["trades"],
                "standing": {
                    name: evaluation.standing[name]
                    for name in (
                        "independent",
                        "consumes",
                        "corrected_since",
                        "may_pass",
                        "reason",
                        "fresh_period",
                    )
                },
            },
            "halt_would_have_fired": {
                "daily_loss_limit": rounded(terms.halts.daily_loss, FRACTION),
                "drawdown_limit": rounded(terms.halts.drawdown, FRACTION),
                **{curve: halt_days_json(days) for curve, days in halts.items()},
            },
            "windows": [
                window_json(number, window, found, terms)
                for number, (window, found) in enumerate(
                    zip(terms.windows, self.windows, strict=True), start=1
                )
            ],
            "stitched_curve": {
                **_span_json(self.stitched),
                "metrics": self.stitched.scores(risk_free_rate=rate).as_json(),
                "boundary_exits": sum(t.exit_reason == "boundary" for t in self.stitched.closed),
            },
            "holdout": {
                **_span_json(self.holdout),
                "parameters": dict(sorted((self.attempt.parameters or {}).items())),
                "metrics": self.holdout.scores(risk_free_rate=rate).as_json(),
                "open_positions": [p.as_json() for p in self.holdout.open],
            },
            "gapped_stops": {c: a.gapped_stops_json() for c, a in accounts.items()},
            "limits": {c: a.limits_json() for c, a in accounts.items()},
            "cost_sensitivity": {
                "impact": str(terms.slippage.impact),
                "safety_margin": str(terms.slippage.margin),
                "rates_as_of": terms.costs_as_of.isoformat(),
                "variants": [
                    {"impact": str(impact), **{c: s.as_json() for c, s in scores.items()}}
                    for impact, scores in self.sensitivity
                ],
            },
            "universe_months": [
                {
                    "month": m.month.isoformat(),
                    "mode": m.mode,
                    "data_cutoff": m.data_cutoff.isoformat(),
                    "cutoff_inferred": m.cutoff_inferred,
                }
                for m in terms.snapshot.months
            ],
            "lineage": _lineage_json(self.lineage),
            "contract_notes": _contract_notes_json(evaluation.contract_notes),
            "closed_positions": {c: [t.as_json() for t in a.closed] for c, a in accounts.items()},
        }

    def text(self) -> str:
        return json.dumps(self.as_json(), indent=2) + "\n"

    def markdown(self) -> str:
        return _Markdown(self).text()


def read_protocol_report(conn: psycopg.Connection, protocol: str) -> ProtocolReport:
    """The report on the Protocol ``protocol`` (its hash, or 12 or more characters of it)
    as the Ledger holds it, its Gate as last evaluated."""
    frozen = _frozen(conn, protocol)
    short = frozen.hash[:12]
    evaluations = queries.gate_evaluations(conn, protocol=frozen.hash)
    if not evaluations:
        raise ValueError(
            f"the Backtest Gate has not been evaluated under Protocol {short};"
            f" `tradeline backtest report --protocol {short}` evaluates it"
        )
    evaluation = evaluations[-1]
    walked, attempt = _finished(conn, frozen)
    windows = tuple(queries.walk_forward_windows(conn, frozen.hash))
    rate = frozen.protocol.risk_free_rate
    by_impact: dict[Decimal, dict[str, Scores]] = {}
    stages = [_stage("Walk-Forward", walked.started_at, [w.recorded_at for w in windows])]
    assert attempt.backtest_id is not None
    holdout_runs = queries.runs(conn, mode="backtest", backtest_id=attempt.backtest_id)
    stages.append(_stage("Holdout", attempt.attempted_at, [r.recorded_at for r in holdout_runs]))
    for found in queries.cost_sensitivities(conn, frozen.hash):
        account = read_account(conn, found.backtest_id)
        by_impact.setdefault(found.impact, {})[found.curve] = account.scores(risk_free_rate=rate)
        runs = queries.runs(conn, mode="backtest", backtest_id=found.backtest_id)
        stages.append(
            _stage(
                f"{CURVE_NAMES[found.curve]} at c = {found.impact}",
                found.started_at,
                [r.recorded_at for r in runs],
            )
        )
    stages.append(Stage("Gate evaluation", evaluation.evaluated_at, evaluation.evaluated_at))
    return ProtocolReport(
        frozen=frozen,
        windows=windows,
        stitched=read_account(conn, walked.backtest_id),
        attempt=attempt,
        holdout=read_account(conn, attempt.backtest_id),
        evaluation=evaluation,
        sensitivity=tuple(sorted(by_impact.items())),
        lineage=queries.lineage(conn, frozen.hash),
        stages=tuple(stages),
    )


@dataclass(frozen=True)
class ProtocolReportResult:
    report: ProtocolReport
    outcome: GateOutcome
    # c sensitivity accounts run (or finished) by this call.
    sensitivity_runs: int
    seconds: float

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": self.report.frozen.hash,
            "outcome": self.outcome.outcome,
            "reason": self.outcome.reason,
            "evaluation": str(self.report.evaluation.id),
            "sensitivity_runs": self.sensitivity_runs,
            "seconds": round(self.seconds, 1),
        }


def report_protocol(
    conn: psycopg.Connection,
    strategy: StrategyFactory,
    *,
    protocol: str | None,
    calendar: TradingCalendar,
    tick_sizes: TickSizes,
    contract_notes: int,
    risk_engine: RiskEngine = RISK_ENGINE,
    fill_model: FillModel = FILL_MODEL,
) -> ProtocolReportResult:
    """Write the c sensitivity the report still lacks, evaluate and record the Backtest Gate
    on the Protocol ``protocol``'s Stitched Curve and Holdout run, and read the report;
    ``contract_notes`` is how many real contract notes check the cost model."""
    began = time.monotonic()
    if not protocol:
        raise ValueError(
            "the report reads a frozen Protocol and names none; name the one whose"
            " Walk-Forward and Holdout have run"
        )
    frozen = _frozen(conn, protocol)
    terms = frozen.protocol
    walked, attempt = _finished(conn, frozen)
    assert attempt.backtest_id is not None
    lengths = {
        "stitched_curve": len(queries.runs(conn, mode="backtest", backtest_id=walked.backtest_id)),
        "holdout": len(queries.runs(conn, mode="backtest", backtest_id=attempt.backtest_id)),
    }
    impacts = (Decimal(0), terms.slippage.impact * 2)
    recorded = {
        (s.curve, s.impact): s.backtest_id for s in queries.cost_sensitivities(conn, frozen.hash)
    }
    pending = []
    for impact in impacts:
        for curve in CURVES:
            account = recorded.get((curve, impact))
            written = (
                [] if account is None else queries.runs(conn, mode="backtest", backtest_id=account)
            )
            if len(written) < lengths[curve]:
                pending.append((curve, impact, account))
    if pending:
        verify_snapshot(conn, frozen, start=terms.first, end=terms.last)
    rerun: dict[str, Callable[..., None]] = {"stitched_curve": restitch, "holdout": rerun_holdout}
    for curve, impact, account in pending:
        rerun[curve](
            conn,
            strategy,
            frozen,
            # Recorded as it starts, so its runtime is its own.
            backtest_id=account or _new_sensitivity(conn, frozen.hash, curve, impact),
            impact=impact,
            calendar=calendar,
            tick_sizes=tick_sizes,
            risk_engine=risk_engine,
            fill_model=fill_model,
        )
        log.info(
            "c sensitivity written",
            extra={"protocol": frozen.hash, "curve": curve, "impact": str(impact)},
        )
    outcome = _evaluate(conn, frozen, walked, attempt, calendar, contract_notes)
    result = ProtocolReportResult(
        read_protocol_report(conn, frozen.hash), outcome, len(pending), time.monotonic() - began
    )
    log.info("protocol report finished", extra=result.summary())
    return result


def _evaluate(
    conn: psycopg.Connection,
    frozen: queries.FrozenProtocol,
    walked: queries.WalkForward,
    attempt: queries.HoldoutAttempt,
    calendar: TradingCalendar,
    contract_notes: int,
) -> GateOutcome:
    """The Backtest Gate on the two curves, recorded with what it read."""
    terms = frozen.protocol
    assert attempt.backtest_id is not None
    curves = {
        "stitched_curve": _metrics(read_account(conn, walked.backtest_id), terms.risk_free_rate),
        "holdout": _metrics(read_account(conn, attempt.backtest_id), terms.risk_free_rate),
    }
    found = standing(conn, frozen.hash, calendar=calendar)
    outcome = evaluate(
        terms.gate,
        stitched=curves["stitched_curve"],
        holdout=curves["holdout"],
        independent=bool(attempt.independent),
        may_pass=found.may_pass,
        standing=found.reason,
    )
    period = found.fresh_period
    standing_json = {
        "independent": attempt.independent,
        "consumes": attempt.consumes,
        "corrected_since": len(attempt.corrected_since),
        "may_pass": found.may_pass,
        "reason": found.reason,
        "fresh_period": None
        if period is None
        else {
            "first": None if period.first is None else period.first.isoformat(),
            "last": None if period.last is None else period.last.isoformat(),
            "trading_days": period.trading_days,
            "twelve_months_to": None
            if period.twelve_months_to is None
            else period.twelve_months_to.isoformat(),
            "long_enough": period.long_enough,
        },
    }
    gate = terms.gate
    with conn.transaction():
        conn.execute(
            "INSERT INTO gate_evaluations (id, gate, protocol, stitched_backtest_id,"
            " holdout_attempt, thresholds, inputs, standing, checks, contract_notes, outcome,"
            " reason) VALUES (%s, 'backtest', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4(),
                frozen.hash,
                walked.backtest_id,
                attempt.id,
                json.dumps(
                    {
                        "sharpe_above": str(gate.sharpe_above),
                        "drawdown_below": str(gate.drawdown_below),
                        "profit_factor_above": str(gate.profit_factor_above),
                        "min_stitched_trades": gate.min_stitched_trades,
                    }
                ),
                json.dumps({curve: _metrics_json(m) for curve, m in curves.items()}),
                json.dumps(standing_json),
                json.dumps(
                    [
                        {
                            "curve": c.curve,
                            "metric": c.metric,
                            "value": None if c.value is None else str(c.value),
                            "rule": c.rule,
                            "threshold": str(c.threshold),
                            "passed": c.passed,
                        }
                        for c in outcome.checks
                    ]
                ),
                contract_notes,
                outcome.outcome,
                outcome.reason,
            ),
        )
    conn.commit()
    return outcome


def _metrics(account: Account, rate: Decimal) -> CurveMetrics:
    scores = account.scores(risk_free_rate=rate)
    return CurveMetrics(scores.trades, scores.sharpe, scores.max_drawdown, scores.profit_factor)


def _metrics_json(metrics: CurveMetrics) -> dict[str, Any]:
    return {
        "trades": metrics.trades,
        "sharpe": None if metrics.sharpe is None else str(metrics.sharpe),
        "max_drawdown": str(metrics.max_drawdown),
        "profit_factor": None if metrics.profit_factor is None else str(metrics.profit_factor),
    }


def _frozen(conn: psycopg.Connection, protocol: str) -> queries.FrozenProtocol:
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(
            f"no Protocol {protocol} is frozen; `tradeline protocol freeze` freezes one"
        )
    return frozen


def _finished(
    conn: psycopg.Connection, frozen: queries.FrozenProtocol
) -> tuple[queries.WalkForward, queries.HoldoutAttempt]:
    """The Walk-Forward and the Holdout run, refused unless both have run to their end."""
    terms, short = frozen.protocol, frozen.hash[:12]
    walked = queries.walk_forward(conn, frozen.hash)
    windows = queries.walk_forward_windows(conn, frozen.hash)
    if walked is None or len(windows) < len(terms.windows):
        raise ValueError(
            f"the Walk-Forward under Protocol {short} has not finished; finish it"
            f" (`tradeline backtest --protocol {short}`) before the report"
        )
    if not queries.runs(conn, mode="backtest", backtest_id=walked.backtest_id):
        raise ValueError(f"no Window of Protocol {short}'s Walk-Forward chose parameters")
    attempt = holdout_run(conn, frozen.hash)
    runs = (
        []
        if attempt is None
        else queries.runs(conn, mode="backtest", backtest_id=attempt.backtest_id)
    )
    if attempt is None or not runs or runs[-1].as_of < terms.holdout.last:
        raise ValueError(
            f"Protocol {short} has not run the Holdout to its last day; run it"
            f" (`tradeline backtest holdout --protocol {short}`) before the report"
        )
    return walked, attempt


def _new_sensitivity(
    conn: psycopg.Connection, protocol: str, curve: str, impact: Decimal
) -> uuid.UUID:
    """A new account for the curve at ``impact``, recorded."""
    backtest_id = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            "INSERT INTO cost_sensitivities (protocol, curve, impact, backtest_id)"
            " VALUES (%s, %s, %s, %s)",
            (protocol, curve, impact, backtest_id),
        )
    conn.commit()
    return backtest_id


def _stage(name: str, started: datetime, writes: Sequence[datetime | None]) -> Stage:
    done = [w for w in writes if w is not None]
    return Stage(name, started, max(done, default=started))


def _span_json(account: Account) -> dict[str, Any]:
    return {
        "from": account.days[0].isoformat(),
        "to": account.days[-1].isoformat(),
        "trading_days": len(account.days),
    }


def _check_json(check: dict[str, Any]) -> dict[str, Any]:
    """A recorded check with its value rounded as the report rounds that metric."""
    value = check["value"]
    if value is not None and check["metric"] != "trades":
        places = FRACTION if check["metric"] == "max_drawdown" else RATIO
        value = rounded(Decimal(value), places)
    elif value is not None:
        value = int(value)
    threshold = check["threshold"]
    return {
        "curve": check["curve"],
        "metric": check["metric"],
        "value": value,
        "rule": check["rule"],
        "threshold": int(threshold) if check["metric"] == "trades" else threshold,
        "passed": check["passed"],
    }


def _lineage_json(lineage: queries.Lineage) -> dict[str, Any]:
    def versions(family: queries.FamilyLineage) -> list[dict[str, Any]]:
        return [
            {
                "protocol": v.protocol,
                "name": v.name,
                "code_revision": v.code_revision,
                "frozen_at": v.frozen_at.astimezone(IST).isoformat(),
                "holdout": None
                if v.holdout is None
                else {
                    "parameters": dict(sorted((v.holdout.parameters or {}).items())),
                    "consumes": v.holdout.consumes,
                    "independent": v.holdout.independent,
                    "corrected_since": len(v.holdout.corrected_since),
                },
            }
            for v in family.versions
        ]

    return {
        "family": lineage.family.family,
        "frozen_at": lineage.frozen_at.astimezone(IST).isoformat(),
        "consumed_by": lineage.family.consumed_by,
        "versions": versions(lineage.family),
        "beside": [
            {"family": f.family, "consumed_by": f.consumed_by, "versions": versions(f)}
            for f in lineage.beside
        ],
    }


def _contract_notes_json(count: int) -> dict[str, Any]:
    if count == 0:
        return {
            "status": "pending",
            "notes": 0,
            "says": "no real Zerodha contract note is available; the cost model's check is"
            " the hand-computed round trips from the dated rate schedule",
        }
    return {
        "status": "tested",
        "notes": count,
        "says": f"{count} real contract notes checked against the rates in force on their dates",
    }


class _Markdown:
    """The report rendered for posting on the Phase 2 issue: the Gate and the Halt days at
    the top, then every section in the JSON's order, then the runtime."""

    def __init__(self, report: ProtocolReport) -> None:
        self._report = report
        self._json = report.as_json()
        self._lines: list[str] = []

    def text(self) -> str:
        data, report = self._json, self._report
        strategy = data["strategy"]
        self._line(f"# Backtest report: {strategy['name']} under Protocol {data['protocol'][:12]}")
        self._line(
            f"Strategy family `{strategy['family']}`, code revision"
            f" `{strategy['code_revision'][:12]}`, Protocol `{data['protocol']}`."
        )
        self._gate()
        self._halts()
        self._windows()
        self._metrics()
        self._gapped()
        self._limits()
        self._sensitivity()
        self._months()
        self._lineage()
        self._section("Contract notes")
        notes = data["contract_notes"]
        self._line(f"**{notes['status']}**: {notes['says']}.")
        self._section("Runtime")
        self._line(
            "Each stage from its first write to the Ledger to its last, so a stage that was"
            " interrupted and resumed includes the pause; the machine's load is not recorded."
        )
        self._table(
            ["Stage", "Started (IST)", "Seconds"],
            [
                [s.name, f"{s.started.astimezone(IST):%Y-%m-%d %H:%M:%S}", f"{s.seconds:.1f}"]
                for s in report.stages
            ],
        )
        return "\n".join(self._lines).rstrip("\n") + "\n"

    def _gate(self) -> None:
        gate = self._json["gate"]
        self._section(f"Backtest Gate: {gate['outcome'].upper()}")
        self._line(gate["reason"][:1].upper() + gate["reason"][1:] + ".")
        thresholds = gate["thresholds"]
        self._line(
            f"Thresholds (the Protocol's): Sharpe above {thresholds['sharpe_above']}, max"
            f" drawdown below {thresholds['drawdown_below']}, profit factor above"
            f" {thresholds['profit_factor_above']} on both curves; at least"
            f" {thresholds['min_stitched_trades']} trades on the Stitched Curve. The Holdout's"
            f" {gate['holdout_trades']} trades are reported, not gated."
        )
        self._table(
            ["Curve", "Metric", "Value", "Rule", "Holds"],
            [
                [
                    CURVE_NAMES.get(c["curve"], "Fresh Period"),
                    c["metric"],
                    _cell(c["value"]),
                    f"{c['rule']} {c['threshold']}",
                    "yes" if c["passed"] else "**no**",
                ]
                for c in gate["checks"]
            ],
        )
        self._line(f"Holdout run: {gate['standing']['reason']}.")

    def _halts(self) -> None:
        halts = self._json["halt_would_have_fired"]
        self._section("Halt would have fired")
        self._line(
            "A backtest never Halts; these are the days the daily-loss Halt (a loss of"
            f" {halts['daily_loss_limit']} of the day before's Equity) or the drawdown Halt"
            f" ({halts['drawdown_limit']} under the peak) would have fired, consecutive days"
            " taken together."
        )
        rows = []
        accounts = self._report._accounts()
        for curve, days in self._report._halts().items():
            for first, last, stretch in _consecutive(accounts[curve].days, days):
                rows.append(
                    [
                        CURVE_NAMES[curve],
                        first.isoformat() if first == last else f"{first} .. {last}",
                        str(len(stretch)),
                        ", ".join(sorted({n for d in stretch for n in d.limits})),
                        str(max(d.daily_loss for d in stretch).quantize(FRACTION)),
                        str(max(d.drawdown for d in stretch).quantize(FRACTION)),
                    ]
                )
        if not rows:
            self._line("None on either curve.")
            return
        self._table(
            ["Curve", "Days", "Count", "Limits", "Worst daily loss", "Deepest drawdown"], rows
        )

    def _windows(self) -> None:
        self._section("Windows")
        rows = []
        for w in self._json["windows"]:
            chosen = w["selection"]
            result = w["out_of_sample_result"]
            rows.append(
                [
                    str(w["window"]),
                    " .. ".join(w["in_sample"]),
                    " .. ".join(w["out_of_sample"]),
                    f"{w['eligible_cells']} of {w['cells']}",
                    "none"
                    if chosen is None
                    else ", ".join(f"{k} = {v}" for k, v in chosen["parameters"].items()),
                    _cell(None if chosen is None else chosen["objective"]),
                    _cell(None if chosen is None else chosen["in_sample_trades"]),
                    str(result["trades"]),
                    _cell(result["return"]),
                ]
            )
        self._table(
            [
                "Window",
                "In-Sample",
                "Out-of-Sample",
                "Eligible cells",
                "Parameters",
                "Objective",
                "In-sample trades",
                "Out-of-sample trades",
                "Out-of-sample return",
            ],
            rows,
        )

    def _metrics(self) -> None:
        data = self._json
        self._section("Stitched Curve and Holdout")
        stitched, holdout = data["stitched_curve"], data["holdout"]
        self._line(
            f"Stitched Curve {stitched['from']} .. {stitched['to']} ({stitched['trading_days']}"
            f" trading days, Boundary Exits {stitched['boundary_exits']}); Holdout"
            f" {holdout['from']} .. {holdout['to']} ({holdout['trading_days']} trading days) at"
            f" {', '.join(f'{k} = {v}' for k, v in holdout['parameters'].items())}."
        )
        self._scores_table({c: data[c]["metrics"] for c in CURVES})

    def _scores_table(self, scores: dict[str, dict[str, Any]]) -> None:
        names = list(scores)
        rows = [
            [label] + [_cell(_at(scores[n], path)) for n in names]
            for label, path in (
                ("Trades", "trades"),
                ("Sharpe", "sharpe"),
                ("Max drawdown", "max_drawdown"),
                ("Profit factor", "profit_factor"),
                ("Expectancy (Rs)", "expectancy.rupees"),
                ("Expectancy (of Equity)", "expectancy.fraction_of_equity"),
                ("Ending Equity (Rs)", "ending_equity"),
                ("Open Positions", "open_positions"),
            )
        ]
        self._table(["Metric", *(CURVE_NAMES.get(n, n) for n in names)], rows)

    def _gapped(self) -> None:
        self._section("Gapped Stops")
        rows = [
            [
                CURVE_NAMES[curve],
                t["symbol"],
                t["opened_on"],
                t["closed_on"],
                str(t["shares"]),
                t["stop"],
                t["exit_price"],
                t["planned_risk"],
                t["realised_loss"],
            ]
            for curve, trades in self._json["gapped_stops"].items()
            for t in trades
        ]
        if not rows:
            self._line("No stop exit filled under its stop on either curve.")
            return
        self._table(
            [
                "Curve",
                "Symbol",
                "Opened",
                "Closed",
                "Shares",
                "Stop",
                "Filled",
                "Planned risk (Rs)",
                "Realised loss (Rs)",
            ],
            rows,
        )

    def _limits(self) -> None:
        self._section("Limits: cap-binding and Unclassified")
        limits = self._json["limits"]
        rows = [
            [label] + [_cell(_at(limits[c], path)) for c in CURVES]
            for label, path in (
                ("Candidates beyond the Passes (entry cap)", "candidates_beyond_passes"),
                (
                    "Passes sized down by the Participation Cap",
                    "participation_cap.passes_sized_down",
                ),
                ("Blocks by the Participation Cap", "participation_cap.blocks"),
                ("Blocks caused by Unclassified", "unclassified.blocks"),
                ("Trades in Unclassified", "unclassified.trades"),
            )
        ]
        self._table(["Count", *(CURVE_NAMES[c] for c in CURVES)], rows)
        for curve in CURVES:
            by_limit = limits[curve]["blocks_by_limit"]
            listed = ", ".join(f"{k} {v}" for k, v in by_limit.items()) or "none"
            self._line(f"{CURVE_NAMES[curve]} Blocks by Limit: {listed}.")

    def _sensitivity(self) -> None:
        self._section("Cost sensitivity")
        sensitivity = self._json["cost_sensitivity"]
        self._line(
            f"The Protocol prices Slippage at a {sensitivity['safety_margin']} safety margin"
            f" plus impact c = {sensitivity['impact']} times the Order's share of the 20-day"
            f" average traded value, on the rates of {sensitivity['rates_as_of']}. As"
            " information only, the Stitched Curve (each Window's chosen parameters, nothing"
            " chosen again) and the Holdout run again with c at zero and at twice its value:"
        )
        data = self._json
        # (curve, c, scores): each curve's runs at every c, the Stitched Curve's first.
        columns = [
            (curve, Decimal(sensitivity["impact"]), data[curve]["metrics"]) for curve in CURVES
        ] + [
            (curve, Decimal(variant["impact"]), variant[curve])
            for variant in sensitivity["variants"]
            for curve in CURVES
        ]
        columns.sort(key=lambda column: (CURVES.index(column[0]), column[1]))
        self._scores_table(
            {f"{CURVE_NAMES[curve]}, c = {impact}": scores for curve, impact, scores in columns}
        )

    def _months(self) -> None:
        months = self._json["universe_months"]
        self._section("Universe months")
        modes = sorted({m["mode"] for m in months})
        counts = ", ".join(f"{sum(m['mode'] == mode for m in months)} {mode}" for mode in modes)
        self._line(f"{len(months)} months read: {counts}.")
        self._table(
            ["Month", "Mode", "Data cutoff", "Cutoff inferred"],
            [
                [m["month"][:7], m["mode"], m["data_cutoff"], _yes(m["cutoff_inferred"])]
                for m in months
            ],
        )

    def _lineage(self) -> None:
        lineage = self._json["lineage"]
        self._section("Lineage")
        consumed = lineage["consumed_by"]
        self._line(
            f"Family `{lineage['family']}`; its Holdout consumed by"
            f" {'no run yet' if consumed is None else f'Protocol `{consumed[:12]}`'}."
        )
        rows = []
        for family in [lineage, *lineage["beside"]]:
            for v in family["versions"]:
                held = v["holdout"]
                rows.append(
                    [
                        family["family"],
                        f"`{v['protocol'][:12]}`",
                        v["name"],
                        v["frozen_at"][:19].replace("T", " "),
                        "not run"
                        if held is None
                        else ("consumed it" if held["consumes"] else "did not consume it")
                        + ("" if held["independent"] else ", Not Independent"),
                    ]
                )
        self._table(["Family", "Protocol", "Strategy", "Frozen (IST)", "Holdout run"], rows)

    def _section(self, title: str) -> None:
        self._lines += [f"## {title}", ""]

    def _line(self, text: str) -> None:
        self._lines += [text, ""]

    def _table(self, header: list[str], rows: list[list[str]]) -> None:
        self._lines.append("| " + " | ".join(header) + " |")
        self._lines.append("|" + "|".join("---" for _ in header) + "|")
        self._lines += ["| " + " | ".join(row) + " |" for row in rows]
        self._lines.append("")


def _at(data: dict[str, Any], path: str) -> Any:
    """The value at a dotted path of nested keys."""
    for key in path.split("."):
        data = data[key]
    return data


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _cell(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _consecutive(days: Sequence[Any], halts: list[HaltDay]) -> list[tuple[Any, Any, list[HaltDay]]]:
    """The Halt days grouped where they fall on consecutive trading days of the curve."""
    index = {day: i for i, day in enumerate(days)}
    found: list[tuple[Any, Any, list[HaltDay]]] = []
    for halt in halts:
        if found and index[halt.day] == index[found[-1][1]] + 1:
            first, _, stretch = found[-1]
            found[-1] = (first, halt.day, [*stretch, halt])
        else:
            found.append((halt.day, halt.day, [halt]))
    return found
