"""The Protocol: how a Strategy version is validated, frozen before any run (ADR 0007).

A Protocol states the parameter grid, the objective and its tie-break, the
Walk-Forward Windows and the Holdout as trading days from the Trading Calendar,
the cost schedule's date with the rates in force on it and Slippage's
parameters, the exit band, every Limit on with its value or off (ADR 0020), the
daily-loss and drawdown Halt fractions a backtest reports against, whether
dividends are credited (stated only when they are), the starting Equity, the
risk-free rate, the Gate thresholds, the Universe Rule version, the Strategy
family, name and code revision, the git commit it was frozen at, and the data
snapshot: each Universe month's mode and build, and the
hash of the Adjusted Series of every member over the Protocol's range.

Its identity is the SHA-256 of its canonical serialisation: JSON with sorted
keys and no spaces, every decimal number written in one fixed form (no
exponent, no trailing zeros, so 2, 2.0 and 2.00 are one number) and as a
string, dates in ISO form. The same content serialised in any key order or
decimal form has the same hash; floats are refused, since they are not exact.

This module is pure. Freezing one (``tradeline.freeze``) reads the data, and
the engine refuses a backtest under none.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tradeline.costs import CHARGES, CostModel, CostSchedule, Rate, Rates, Slippage
from tradeline.dates import month_end, month_of, next_month
from tradeline.metrics import HaltLimits
from tradeline.risk import Limits
from tradeline.trading_calendar import TradingCalendar

# The layout of the serialised content; a change to it is a new format. Format 2 adds the
# Halt fractions (#84).
FORMAT = 2
OBJECTIVE = (
    "in-sample Sharpe after costs, averaged with the cell's grid neighbours along each axis;"
    " a cell with fewer than min_in_sample_trades in-sample trades is ineligible"
)
# The one Limit a Protocol states by leaving it out, when on (``limits_content``).
STOP_REQUIRED = "stop_required"
TIE_BREAK = ("higher raw Sharpe", "more trades", "smaller k", "smaller r", "smaller n")


@dataclass(frozen=True)
class Span:
    """Trading days ``first`` to ``last``, both open days on the Trading Calendar."""

    first: date
    last: date


@dataclass(frozen=True)
class Window:
    in_sample: Span
    out_of_sample: Span


@dataclass(frozen=True)
class Layout:
    """Where the Windows fall, in months: the Holdout from ``holdout_start`` (a first day)
    for ``holdout_months``, and ``windows`` Out-of-Sample Windows of
    ``out_of_sample_months`` anchored back from it, each with the ``in_sample_months``
    before it as its In-Sample Window."""

    holdout_start: date
    holdout_months: int
    windows: int
    out_of_sample_months: int
    in_sample_months: int

    def __post_init__(self) -> None:
        if self.holdout_start.day != 1:
            raise ValueError(f"the Holdout starts on a month's first day, not {self.holdout_start}")
        for name in ("holdout_months", "windows", "out_of_sample_months", "in_sample_months"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


@dataclass(frozen=True)
class Gate:
    """The Backtest Gate's thresholds (``docs/risk-controls.md``)."""

    sharpe_above: Decimal
    drawdown_below: Decimal
    profit_factor_above: Decimal
    min_stitched_trades: int


@dataclass(frozen=True)
class Terms:
    """What the Operator chooses for a Protocol: everything in it but the costs and Limits,
    the Strategy, the commit and the data."""

    # Each free parameter's values, by the name the Strategy's ``parameters`` gives it.
    grid: Mapping[str, tuple[Decimal, ...]]
    min_in_sample_trades: int
    layout: Layout
    band: Decimal
    starting_equity: Decimal
    risk_free_rate: Decimal
    gate: Gate
    # Trading days before the first Window whose Adjusted Series the snapshot also hashes:
    # the bars the first Runs read.
    warm_up_trading_days: int
    # Whether a holder is credited each dividend in cash on its Ex-Date (ADR 0020).
    dividends: bool = False


@dataclass(frozen=True)
class MonthSnapshot:
    """One Universe month as the Protocol froze it: its mode and its build (ADR 0005)."""

    month: date
    mode: str
    window_start: date
    data_cutoff: date
    eod_run_ids: tuple[uuid.UUID, ...]
    instruments_run_id: uuid.UUID | None
    eod_hash: str
    links_hash: str
    cutoff_inferred: bool


@dataclass(frozen=True)
class DataSnapshot:
    """The data a Protocol's runs read: each Universe month in its range, and the hash of
    each member's Adjusted Series from ``start`` to ``end`` as of ``end``, by the
    Instrument (or, under a Symbol-ranked rule, the Symbol) its Runs read it by."""

    start: date
    end: date
    months: tuple[MonthSnapshot, ...]
    adjusted_series: Mapping[str, str]


@dataclass(frozen=True)
class Protocol:
    family: str
    name: str
    code_revision: str
    git_commit: str
    grid: Mapping[str, tuple[Decimal, ...]]
    objective: str
    min_in_sample_trades: int
    tie_break: tuple[str, ...]
    windows: tuple[Window, ...]
    holdout: Span
    costs_as_of: date
    rates: Rates
    slippage: Slippage
    band: Decimal
    limits: Limits
    # The Halts a backtest never simulates but reports the days of (``docs/risk-controls.md``).
    halts: HaltLimits
    starting_equity: Decimal
    risk_free_rate: Decimal
    gate: Gate
    rule_version: str
    warm_up_trading_days: int
    snapshot: DataSnapshot
    # Whether a holder at the close before a dividend's Ex-Date is credited it in cash on the
    # Ex-Date (ADR 0020). Stated in the content only when on, so a Protocol frozen before
    # it existed (``pullback_v1``'s) reads back to the same hash, with dividends off.
    dividends: bool = False

    @property
    def first(self) -> date:
        """The first trading day any of its runs may Run: the first In-Sample day."""
        return self.windows[0].in_sample.first

    @property
    def last(self) -> date:
        """The Holdout's last trading day."""
        return self.holdout.last

    @property
    def hash(self) -> str:
        return content_hash(self.content())

    def cost_model(self) -> CostModel:
        """The rates in force on the schedule's date for every Fill, with its Slippage."""
        schedule = CostSchedule(
            tuple(
                Rate(self.costs_as_of, charge, getattr(self.rates, charge), "Protocol")
                for charge in CHARGES
            )
        )
        return CostModel(schedule, self.slippage, rates_as_of=self.costs_as_of)

    def content(self) -> dict[str, Any]:
        snapshot = self.snapshot
        content: dict[str, Any] = {
            "format": FORMAT,
            "strategy": {
                "family": self.family,
                "name": self.name,
                "code_revision": self.code_revision,
            },
            "git_commit": self.git_commit,
            "grid": {name: list(values) for name, values in self.grid.items()},
            "objective": {
                "rule": self.objective,
                "min_in_sample_trades": self.min_in_sample_trades,
            },
            "tie_break": list(self.tie_break),
            "windows": [
                {"in_sample": _span(w.in_sample), "out_of_sample": _span(w.out_of_sample)}
                for w in self.windows
            ],
            "holdout": _span(self.holdout),
            "costs": {
                "schedule_as_of": self.costs_as_of,
                "rates": dataclasses.asdict(self.rates),
                "slippage": dataclasses.asdict(self.slippage),
            },
            "band": self.band,
            "limits": limits_content(self.limits),
            "halts": dataclasses.asdict(self.halts),
            "starting_equity": self.starting_equity,
            "risk_free_rate": self.risk_free_rate,
            "gate": dataclasses.asdict(self.gate),
            "universe_rule_version": self.rule_version,
            "warm_up_trading_days": self.warm_up_trading_days,
            "data_snapshot": {
                "start": snapshot.start,
                "end": snapshot.end,
                "months": [dataclasses.asdict(m) for m in snapshot.months],
                "adjusted_series": dict(snapshot.adjusted_series),
            },
        }
        if self.dividends:
            content["dividends"] = True
        return content

    @classmethod
    def from_content(cls, content: Mapping[str, Any]) -> Protocol:
        """The Protocol a serialised content states."""
        if content["format"] != FORMAT:
            raise ValueError(f"Protocol format {content['format']} is not {FORMAT}")
        costs, snapshot = content["costs"], content["data_snapshot"]
        return cls(
            family=content["strategy"]["family"],
            name=content["strategy"]["name"],
            code_revision=content["strategy"]["code_revision"],
            git_commit=content["git_commit"],
            grid={
                name: tuple(Decimal(v) for v in values) for name, values in content["grid"].items()
            },
            objective=content["objective"]["rule"],
            min_in_sample_trades=content["objective"]["min_in_sample_trades"],
            tie_break=tuple(content["tie_break"]),
            windows=tuple(
                Window(_read_span(w["in_sample"]), _read_span(w["out_of_sample"]))
                for w in content["windows"]
            ),
            holdout=_read_span(content["holdout"]),
            costs_as_of=date.fromisoformat(costs["schedule_as_of"]),
            rates=Rates(**{name: Decimal(v) for name, v in costs["rates"].items()}),
            slippage=Slippage(**{name: Decimal(v) for name, v in costs["slippage"].items()}),
            band=Decimal(content["band"]),
            limits=read_limits(content["limits"]),
            halts=HaltLimits(**{name: Decimal(v) for name, v in content["halts"].items()}),
            starting_equity=Decimal(content["starting_equity"]),
            risk_free_rate=Decimal(content["risk_free_rate"]),
            gate=Gate(
                sharpe_above=Decimal(content["gate"]["sharpe_above"]),
                drawdown_below=Decimal(content["gate"]["drawdown_below"]),
                profit_factor_above=Decimal(content["gate"]["profit_factor_above"]),
                min_stitched_trades=content["gate"]["min_stitched_trades"],
            ),
            rule_version=content["universe_rule_version"],
            warm_up_trading_days=content["warm_up_trading_days"],
            snapshot=DataSnapshot(
                start=date.fromisoformat(snapshot["start"]),
                end=date.fromisoformat(snapshot["end"]),
                months=tuple(_read_month(m) for m in snapshot["months"]),
                adjusted_series=dict(snapshot["adjusted_series"]),
            ),
            dividends=content.get("dividends", False),
        )

    def boundaries_text(self) -> str:
        """The Windows and the Holdout as trading days, for the Operator to check."""
        return windows_text(self.windows, self.holdout)


def windows_text(windows: Sequence[Window], holdout: Span) -> str:
    lines = [f"{'Window':<8}{'In-Sample':<26}Out-of-Sample"]
    for number, window in enumerate(windows, start=1):
        lines.append(
            f"{number:<8}{_span_text(window.in_sample):<26}{_span_text(window.out_of_sample)}"
        )
    lines.append(f"{'Holdout':<8}{_span_text(holdout)}")
    return "\n".join(lines) + "\n"


def _span_text(span: Span) -> str:
    return f"{span.first} .. {span.last}"


def window_boundaries(layout: Layout, calendar: TradingCalendar) -> tuple[tuple[Window, ...], Span]:
    """The Windows and the Holdout of ``layout`` as their first and last trading days."""
    out_of_sample = []
    start = _add_months(layout.holdout_start, -layout.windows * layout.out_of_sample_months)
    for _ in range(layout.windows):
        out_of_sample.append(start)
        start = _add_months(start, layout.out_of_sample_months)
    windows = tuple(
        Window(
            in_sample=_trading_span(
                calendar, _add_months(oos, -layout.in_sample_months), layout.in_sample_months
            ),
            out_of_sample=_trading_span(calendar, oos, layout.out_of_sample_months),
        )
        for oos in out_of_sample
    )
    return windows, _trading_span(calendar, layout.holdout_start, layout.holdout_months)


def _trading_span(calendar: TradingCalendar, month: date, months: int) -> Span:
    last_month = _add_months(month, months - 1)
    days = list(calendar.trading_days(month, month_end(last_month)))
    if not days:
        raise ValueError(f"no trading day from {month:%Y-%m} to {last_month:%Y-%m}")
    return Span(days[0], days[-1])


def _add_months(month: date, count: int) -> date:
    index = month.year * 12 + month.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def months_between(first: date, last: date) -> list[date]:
    """Every month from the one ``first`` falls in to the one ``last`` falls in."""
    months, month = [], month_of(first)
    while month <= last:
        months.append(month)
        month = next_month(month)
    return months


def canonical(content: Mapping[str, Any]) -> str:
    """The one serialisation of ``content``: sorted keys, no spaces, fixed decimals."""
    return json.dumps(
        _normal(content), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def content_hash(content: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical serialisation, in hex: the Protocol's key."""
    return hashlib.sha256(canonical(content).encode()).hexdigest()


def _normal(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"a Protocol's keys are strings, not {key!r}")
        return {key: _normal(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normal(item) for item in value]
    if isinstance(value, Decimal):
        return fixed_decimal(value)
    if isinstance(value, float):
        raise TypeError(f"{value!r} is a float, which is not exact; give a Decimal")
    if isinstance(value, datetime):
        raise TypeError(f"{value!r} is a time; a Protocol holds dates")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    raise TypeError(f"a Protocol cannot hold {type(value).__name__} {value!r}")


def fixed_decimal(value: Decimal) -> str:
    """``value`` with no exponent and no trailing zeros: 2, 2.0 and 2.00 are all ``2``."""
    if not value.is_finite():
        raise ValueError(f"{value} is not a finite number")
    if value == 0:
        return "0"
    if value == value.to_integral_value():
        return f"{value.quantize(Decimal(1)):f}"
    return f"{value.normalize():f}"


def limits_content(limits: Limits) -> dict[str, Any]:
    """Every Limit as a Protocol states it: its value, or null when off. ``stop_required``
    appears only when off: every Protocol frozen before Limits were declared (ADR 0020)
    required a stop and does not name it, so its absence is the stop required, and those
    Protocols serialise, and hash, as they were frozen."""
    limits.require_stated()
    content = {f.name: getattr(limits, f.name) for f in dataclasses.fields(limits)}
    if content.pop(STOP_REQUIRED):
        return content
    return {**content, STOP_REQUIRED: False}


def read_limits(raw: Mapping[str, Any]) -> Limits:
    """The Limits a Protocol states, refusing one it leaves unstated: a count stays an
    integer, a fraction or an amount is a decimal, and null is off."""
    stated = dict(raw)
    if stated.get(STOP_REQUIRED, False) is not False:
        raise ValueError(
            f"a Protocol states {STOP_REQUIRED} by leaving it out, or as false when off,"
            f" not as {stated[STOP_REQUIRED]!r}"
        )
    stop_required = stated.pop(STOP_REQUIRED, True)
    names = {f.name for f in dataclasses.fields(Limits)}
    unknown = sorted(set(stated) - names)
    if unknown:
        raise ValueError(f"a Protocol states no Limit named {', '.join(unknown)}")
    values: dict[str, Any] = {
        name: v if v is None or isinstance(v, int) else Decimal(v) for name, v in stated.items()
    }
    limits = Limits(stop_required=stop_required, **values)
    limits.require_stated()
    return limits


def _span(span: Span) -> dict[str, date]:
    return {"first": span.first, "last": span.last}


def _read_span(raw: Mapping[str, str]) -> Span:
    return Span(date.fromisoformat(raw["first"]), date.fromisoformat(raw["last"]))


def _read_month(raw: Mapping[str, Any]) -> MonthSnapshot:
    run = raw["instruments_run_id"]
    return MonthSnapshot(
        month=date.fromisoformat(raw["month"]),
        mode=raw["mode"],
        window_start=date.fromisoformat(raw["window_start"]),
        data_cutoff=date.fromisoformat(raw["data_cutoff"]),
        eod_run_ids=tuple(uuid.UUID(r) for r in raw["eod_run_ids"]),
        instruments_run_id=None if run is None else uuid.UUID(run),
        eod_hash=raw["eod_hash"],
        links_hash=raw["links_hash"],
        cutoff_inferred=raw["cutoff_inferred"],
    )
