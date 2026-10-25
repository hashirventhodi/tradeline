"""Non-secret settings, loaded from ``tradeline.toml``.

Each section is a frozen dataclass with defaults; unknown sections, unknown
keys, and wrong types are refused by name so a typo cannot silently fall back
to a default. Secrets never belong here (see ``tradeline.secrets``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tomllib
import types
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar, Literal, get_args, get_origin, get_type_hints

from tradeline.costs import Slippage
from tradeline.dates import parse_month
from tradeline.metrics import HaltLimits
from tradeline.protocol import BenchmarkGate, Gate, Layout, Terms
from tradeline.risk import Limits

CONFIG_PATH_VAR = "TRADELINE_CONFIG"
# Every Limit a family's Limit set states, in ``Limits``' order.
_LIMIT_NAMES = tuple(f.name for f in fields(Limits))
DEFAULT_CONFIG_PATH = Path("tradeline.toml")


class ConfigError(ValueError):
    pass


# ``lowvol_v1``'s buffer against NSE's (``tradeline.strategy.lowvol``), noted in its Protocol.
BUFFER_SCALE_NOTE = (
    "the buffer of 60 is NSE's NIFTY100 Low Volatility 30 rule of twice the count, kept"
    " literally; NSE applies it to about 100 eligible names, and lowvol_v1's eligible set"
    " (about 150 to 200 of the Universe) is larger, so a held name stays among a smaller"
    " share of the eligible than in the index"
)

# Why ``lowvol_v1``'s Holdout run is Not Independent though it is its family's first (ADR
# 0007 amendment of 2026-10-25), frozen with its Protocol.
NOT_INDEPENDENT_REASON = (
    "the family was chosen after reading published results covering the Holdout's years"
    " (NSE's factor indices among them)"
)


RankBy = Literal["symbol", "instrument"]
RANK_BY: tuple[RankBy, ...] = get_args(RankBy)
ListingAgeFrom = Literal["listing_date", "earliest_evidence"]
LISTING_AGE_FROM: tuple[ListingAgeFrom, ...] = get_args(ListingAgeFrom)


@dataclass(frozen=True)
class UniverseRule:
    """The Universe Rule's parameters (ADR 0005); see ``tradeline.universe``.

    Every field takes part in ``version``, so any change to the rule is a new
    rule version and the membership it produces is recorded under it. The one
    exceptions are ``rank_by`` and ``listing_age_from`` at their original
    values, ``symbol`` and ``listing_date``: each is left out of the
    parameters, so the rule as it was before Instruments (ADR 0016), and before
    Listing Age counted from the earliest evidence (ADR 0017), keeps the
    version its memberships were stored under.
    """

    top_n: int = 200
    # The trailing window, in trading days before the month's first trading day.
    lookback_trading_days: int = 126
    # Trading days of Listing Age before the month's first trading day.
    min_listing_trading_days: int = 126
    # Rows required in the window, as a whole percentage of its trading days.
    min_presence_percent: int = 90
    # The only series eligible: EQ excludes SME (SM, ST) and trade-for-trade (BE, BZ).
    series: str = "EQ"
    # ETFs and other fund units trade in series EQ; their ISINs begin with INF.
    excluded_isin_prefix: str = "INF"
    # What is ranked: each Symbol on its own, or each Instrument across its
    # Symbols (ADR 0016), so a rename inside the window is not a new listing.
    rank_by: RankBy = "symbol"
    # What Listing Age counts from: the Instrument's listing date, or the
    # earlier of it and the Instrument's first row (ADR 0017), which needs
    # Instruments to be ranked.
    listing_age_from: ListingAgeFrom = "listing_date"

    def __post_init__(self) -> None:
        if self.rank_by not in RANK_BY:
            raise ConfigError(f"universe.rank_by must be one of {', '.join(RANK_BY)}")
        if self.listing_age_from not in LISTING_AGE_FROM:
            raise ConfigError(
                f"universe.listing_age_from must be one of {', '.join(LISTING_AGE_FROM)}"
            )
        if self.listing_age_from == "earliest_evidence" and self.rank_by != "instrument":
            raise ConfigError(
                'universe.listing_age_from = "earliest_evidence" needs rank_by = "instrument"'
            )
        for name in ("top_n", "lookback_trading_days", "min_listing_trading_days"):
            if getattr(self, name) < 1:
                raise ConfigError(f"universe.{name} must be at least 1")
        if not 0 < self.min_presence_percent <= 100:
            raise ConfigError("universe.min_presence_percent must be between 1 and 100")

    @property
    def parameters(self) -> dict[str, Any]:
        parameters = dataclasses.asdict(self)
        if self.rank_by == "symbol":
            del parameters["rank_by"]
        if self.listing_age_from == "listing_date":
            del parameters["listing_age_from"]
        return parameters

    @property
    def version(self) -> str:
        """The parameter hash: SHA-256 of the parameters as canonical JSON."""
        canonical = json.dumps(self.parameters, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Config:
    @dataclass(frozen=True)
    class Logging:
        level: str = "INFO"

    @dataclass(frozen=True)
    class News:
        # Feed name -> RSS URL. The name is what each stored item records as its feed.
        feeds: dict[str, str] = field(default_factory=dict)

    @dataclass(frozen=True)
    class Parity:
        # A candle's close differs from the end-of-day close when it is further
        # from it, once Kite's own adjustment factor is taken out, than this
        # fraction of the price plus half of Kite's 5 paisa tick.
        close_tolerance: float = 0.001
        # Kite Parity passes only when the differences no Parity Reason
        # explains are under this percentage of the rows compared; a run
        # exactly at it fails (Phase 1: under 0.1, ADR 0018).
        max_unexplained_pct: float = 0.1
        # The Trading Calendar whose Special Sessions explain a missing candle:
        # the same file as ``integrity.trading_calendar`` (a config naming two
        # is refused), relative to the working directory.
        trading_calendar: str = "trading_calendar.csv"

    @dataclass(frozen=True)
    class Integrity:
        # The checked-in Trading Calendar (``tradeline.trading_calendar``),
        # relative to the working directory.
        trading_calendar: str = "trading_calendar.csv"
        # The checked-in Large Move records (``tradeline.large_move_records``),
        # relative to the working directory.
        large_moves: str = "large_moves.csv"
        # A close more than this many times the previous close, either way,
        # is a Large Move.
        large_move_ratio: float = 1.5

        def __post_init__(self) -> None:
            if not self.large_move_ratio > 1:
                raise ConfigError("integrity.large_move_ratio must be more than 1")

    @dataclass(frozen=True)
    class Instruments:
        # The checked-in file of manual Instrument Links (``tradeline.manual_links``),
        # relative to the working directory.
        manual_links: str = "manual_links.csv"

    @dataclass(frozen=True)
    class CorporateActions:
        # The checked-in file of splits and bonuses cited from NSE's circulars
        # (``tradeline.cited_actions``), relative to the working directory.
        cited: str = "cited_corporate_actions.csv"
        # The checked-in demerger table (``tradeline.demergers``), relative to the working
        # directory; stored by ``ingest demergers``.
        demergers: str = "demergers.csv"
        # The checked-in list of unparsed dividend subjects, each with the Operator's reason
        # (``tradeline.dividends.load_listed_dividends``), read by the pre-Holdout checks.
        unparsed_dividends: str = "unparsed_dividends.csv"

    @dataclass(frozen=True)
    class Benchmark:
        """The Benchmark and the factor fund (ADR 0015 amendment, ``tradeline.benchmark``)."""

        # The checked-in file naming the Benchmark's AMFI scheme from each recorded
        # fund-change event, relative to the working directory. Nothing else names it.
        funds: str = "benchmark_funds.csv"
        # AMFI's code of the fund for the same factor, reported beside the Benchmark and
        # never gated: the ICICI Prudential Nifty 100 Low Vol 30 ETF.
        factor_fund: int = 141596
        # The checked-in file of its unit splits, which AMFI's NAVs are not adjusted for
        # (``tradeline.benchmark.load_unit_splits``), relative to the working directory.
        unit_splits: str = "fund_unit_splits.csv"

    @dataclass(frozen=True)
    class Tax:
        """The tax ledger's inputs (``tradeline.tax``), frozen into a Protocol that keeps tax."""

        # The checked-in dated tax-rate schedule, relative to the working directory.
        schedule: str = "tax_rates.csv"
        # Dividends' rate, cess included: the 30 percent slab plus cess.
        dividend_rate: float = 0.312

    @dataclass(frozen=True)
    class LimitSet:
        """One family's Limits of ``docs/risk-controls.md`` (``tradeline.risk``), fractions
        of Equity where they are, each on with its value or off, written ``false`` (ADR
        0020); and the Halt fractions a backtest reports. The defaults are ``pullback_v1``'s
        set, every Limit on."""

        # The section's name in ``tradeline.toml``, for the errors.
        SECTION: ClassVar[str] = "risk"

        stop_required: bool = True
        risk_per_trade: float | bool = 0.01
        max_open_positions: int | bool = 8
        max_position: float | bool = 0.15
        max_sector: float | bool = 0.30
        max_gross: float | bool = 1.0
        # Of the 20-day average daily traded value.
        participation_cap: float | bool = 0.01
        # Rupees.
        min_order_value: int | bool = 5000
        max_new_entries: int | bool = 4
        min_reward_to_risk: float | bool = 1.5
        # The Halts a backtest reports and never applies: a day's loss on the day before's
        # Equity, and the fall from the Equity peak, each false when off.
        daily_loss_halt: float | bool = 0.03
        drawdown_halt: float | bool = 0.10
        # The Relative Drawdown Halt (ADR 0020): its threshold is not configured but
        # computed when the Protocol is frozen (``tradeline.index_history``).
        relative_drawdown_halt: bool = False

        def __post_init__(self) -> None:
            for name in (*_LIMIT_NAMES, "daily_loss_halt", "drawdown_halt"):
                if name != "stop_required" and getattr(self, name) is True:
                    raise ConfigError(
                        f"{self.SECTION}.{name} is a Limit's value, or false when it is off"
                    )
            try:
                self.limits()
            except ValueError as error:
                raise ConfigError(f"{self.SECTION}: {error}") from error

        def limits(self) -> Limits:
            def stated(name: str) -> Decimal | int | None:
                value = getattr(self, name)
                if value is False:
                    return None
                return value if isinstance(value, int) else Decimal(str(value))

            counts = {"max_open_positions", "max_new_entries"}
            values: dict[str, Any] = {
                name: stated(name) for name in _LIMIT_NAMES if name != "stop_required"
            }
            for name, value in values.items():
                if name not in counts and isinstance(value, int):
                    values[name] = Decimal(value)
            return Limits(stop_required=self.stop_required, **values)

        def halts(self) -> HaltLimits:
            """The daily-loss and drawdown Halts; the Relative Drawdown Halt's threshold is
            added when the Protocol is frozen."""

            def fraction(value: float | bool) -> Decimal | None:
                return None if value is False else Decimal(str(value))

            return HaltLimits(
                daily_loss=fraction(self.daily_loss_halt), drawdown=fraction(self.drawdown_halt)
            )

    @dataclass(frozen=True)
    class Risk(LimitSet):
        """``pullback_v1``'s Limits and Halts (``LimitSet``), and the tick table every
        Plan is priced on."""

        # The checked-in dated table of NSE tick sizes (``tradeline.tick_sizes``),
        # relative to the working directory.
        tick_sizes: str = "tick_sizes.csv"

    @dataclass(frozen=True)
    class LowvolRisk(LimitSet):
        """``lowvol_v1``'s Limits (``docs/risk-controls.md``, "Limits by family"): a
        portfolio Plan has no stop, so the Limits read off one are off, and so are the
        entry cap and the sector Limit. The daily-loss and drawdown Halts are off, replaced
        by the Relative Drawdown Halt (ADR 0020)."""

        SECTION: ClassVar[str] = "lowvol.risk"

        stop_required: bool = False
        risk_per_trade: float | bool = False
        max_open_positions: int | bool = 30
        max_position: float | bool = 0.15
        max_sector: float | bool = False
        max_gross: float | bool = 1.0
        participation_cap: float | bool = 0.01
        min_order_value: int | bool = 5000
        max_new_entries: int | bool = False
        min_reward_to_risk: float | bool = False
        daily_loss_halt: float | bool = False
        drawdown_halt: float | bool = False
        relative_drawdown_halt: bool = True

    @dataclass(frozen=True)
    class LowvolProtocol:
        """What the Operator states in ``lowvol_v1``'s Protocol beyond its Limits and costs.
        Its grid has one cell (``lowvol_v1`` has no free parameter), so Walk-Forward selects
        nothing and its Out-of-Sample Windows run as one continuous backtest, the Stitched
        Curve (``tradeline.walk_forward``); no In-Sample run is made, and the one In-Sample
        month before the first Window holds the decision day of its Initial Buy."""

        holdout_start: str = "2024-10"
        holdout_months: int = 24
        windows: int = 5
        out_of_sample_months: int = 12
        in_sample_months: int = 1
        starting_equity: int = 500000
        risk_free_rate: float = 0.065
        # Over the close for a Rebalance's buy, under it for its sale.
        band: float = 0.01
        # 253 trading days of bars before the Initial Buy's decision day, and a margin.
        warm_up_trading_days: int = 260
        # The Backtest Gate against the Benchmark (ADR 0015 amendment): on both curves the
        # annualised excess of Liquidation Value over the Benchmark's above this, and its
        # drawdown at most the Benchmark's plus the margin; this many Rebalances on the
        # Stitched Curve at least.
        excess_above: float = 0.0
        drawdown_margin: float = 0.02
        min_stitched_rebalances: int = 8
        # The tax ledger, and the Liquidation Value against the Benchmark (ADR 0015
        # amendment); the Benchmark Gate needs it.
        tax: bool = True
        # Dividends credited in cash on the Ex-Date, and the demerger table's Demerger
        # Credits (ADR 0020): the Benchmark is a Total Return comparison.
        dividends: bool = True
        demergers: bool = True
        # What the Protocol notes of the rule beyond its numbers (``Protocol.notes``).
        buffer_scale_note: str = BUFFER_SCALE_NOTE
        # Why its Holdout run is Not Independent, frozen with the Protocol: it then passes the
        # Backtest Gate only with a Fresh Period. Required: the family was chosen after
        # reading results that cover the Holdout's years.
        not_independent: str = NOT_INDEPENDENT_REASON

        def __post_init__(self) -> None:
            if not self.not_independent.strip():
                raise ConfigError(
                    "lowvol.protocol: not_independent states why lowvol_v1's Holdout run is"
                    " Not Independent (its family was chosen after reading results covering"
                    " the Holdout's years), so it cannot be empty"
                )
            if not self.tax:
                raise ConfigError(
                    "lowvol.protocol: the Benchmark Gate compares Liquidation Values after"
                    " tax, so tax = false is refused"
                )
            try:
                self.terms()
            except (ValueError, InvalidOperation) as error:
                raise ConfigError(f"lowvol.protocol: {error}") from error

        def terms(self) -> Terms:
            return Terms(
                grid={},
                min_in_sample_trades=0,
                layout=Layout(
                    holdout_start=parse_month(self.holdout_start),
                    holdout_months=self.holdout_months,
                    windows=self.windows,
                    out_of_sample_months=self.out_of_sample_months,
                    in_sample_months=self.in_sample_months,
                ),
                band=Decimal(str(self.band)),
                starting_equity=Decimal(self.starting_equity),
                risk_free_rate=Decimal(str(self.risk_free_rate)),
                gate=BenchmarkGate(
                    excess_above=Decimal(str(self.excess_above)),
                    drawdown_margin=Decimal(str(self.drawdown_margin)),
                    min_stitched_rebalances=self.min_stitched_rebalances,
                ),
                warm_up_trading_days=self.warm_up_trading_days,
                tax=self.tax,
                dividends=self.dividends,
                demergers=self.demergers,
                notes={"buffer_scale": self.buffer_scale_note},
                not_independent=self.not_independent,
            )

    @dataclass(frozen=True)
    class Lowvol:
        """``lowvol_v1``'s Limits and Protocol (``[lowvol.risk]``, ``[lowvol.protocol]``),
        which ``tradeline protocol freeze --strategy lowvol_v1`` freezes."""

        risk: Config.LowvolRisk = field(default_factory=lambda: Config.LowvolRisk())
        protocol: Config.LowvolProtocol = field(default_factory=lambda: Config.LowvolProtocol())

    @dataclass(frozen=True)
    class Costs:
        """The cost model (``tradeline.costs``): the checked-in dated rate schedule, the real
        contract notes it is checked against, and Slippage's parameters."""

        # Relative to the working directory.
        schedule: str = "cost_rates.csv"
        contract_notes: str = "contract_notes.csv"
        # Of the Order's value, per side.
        safety_margin: float = 0.0005
        # c in c x (Order value / 20-day average daily traded value).
        impact: float = 0.1

        def slippage(self) -> Slippage:
            return Slippage(
                margin=Decimal(str(self.safety_margin)), impact=Decimal(str(self.impact))
            )

    @dataclass(frozen=True)
    class Protocol:
        """What the Operator states in a Protocol (``tradeline.protocol``) beyond the Limits
        and costs: the Windows, the grid, the objective's minimum trades, the band, the
        starting Equity, the risk-free rate and the Gate thresholds (#72)."""

        # The Holdout: from this month (YYYY-MM), this many months.
        holdout_start: str = "2024-10"
        holdout_months: int = 24
        # Out-of-Sample Windows anchored back from the Holdout, each with its In-Sample
        # Window before it.
        windows: int = 5
        out_of_sample_months: int = 12
        in_sample_months: int = 24
        # The grid, each value a decimal string so it is exact.
        k: list[str] = field(default_factory=lambda: ["1.5", "2", "2.5", "3"])
        r: list[str] = field(default_factory=lambda: ["1.5", "2", "3"])
        n: list[int] = field(default_factory=lambda: [5, 10, 15])
        # A cell with fewer in-sample trades is ineligible.
        min_in_sample_trades: int = 30
        # Rupees, at the start of the Stitched Curve, of the Holdout and of each In-Sample run.
        starting_equity: int = 500000
        # A year, for Sharpe's excess returns.
        risk_free_rate: float = 0.065
        # Under the close, the limit of a stop or time-stop exit.
        band: float = 0.05
        # Trading days before the first Window whose Adjusted Series is also hashed.
        warm_up_trading_days: int = 250
        # The Backtest Gate.
        sharpe_above: float = 1.0
        drawdown_below: float = 0.15
        profit_factor_above: float = 1.3
        min_stitched_trades: int = 150

        def __post_init__(self) -> None:
            try:
                self.terms()
            except (ValueError, InvalidOperation) as error:
                raise ConfigError(f"protocol: {error}") from error

        def terms(self) -> Terms:
            def exact(values: list[str]) -> tuple[Decimal, ...]:
                return tuple(Decimal(v) for v in values)

            return Terms(
                grid={"k": exact(self.k), "r": exact(self.r), "n": tuple(map(Decimal, self.n))},
                min_in_sample_trades=self.min_in_sample_trades,
                layout=Layout(
                    holdout_start=parse_month(self.holdout_start),
                    holdout_months=self.holdout_months,
                    windows=self.windows,
                    out_of_sample_months=self.out_of_sample_months,
                    in_sample_months=self.in_sample_months,
                ),
                band=Decimal(str(self.band)),
                starting_equity=Decimal(self.starting_equity),
                risk_free_rate=Decimal(str(self.risk_free_rate)),
                gate=Gate(
                    sharpe_above=Decimal(str(self.sharpe_above)),
                    drawdown_below=Decimal(str(self.drawdown_below)),
                    profit_factor_above=Decimal(str(self.profit_factor_above)),
                    min_stitched_trades=self.min_stitched_trades,
                ),
                warm_up_trading_days=self.warm_up_trading_days,
            )

    logging: Logging = field(default_factory=Logging)
    universe: UniverseRule = field(default_factory=UniverseRule)
    parity: Parity = field(default_factory=Parity)
    news: News = field(default_factory=News)
    instruments: Instruments = field(default_factory=Instruments)
    corporate_actions: CorporateActions = field(default_factory=CorporateActions)
    benchmark: Benchmark = field(default_factory=Benchmark)
    tax: Tax = field(default_factory=Tax)
    integrity: Integrity = field(default_factory=Integrity)
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    protocol: Protocol = field(default_factory=Protocol)
    lowvol: Lowvol = field(default_factory=Lowvol)

    def __post_init__(self) -> None:
        # One Trading Calendar serves both checks (ADR 0018); two paths could drift apart.
        parity, integrity = self.parity.trading_calendar, self.integrity.trading_calendar
        if parity != integrity:
            raise ConfigError(
                f"parity.trading_calendar ({parity}) and integrity.trading_calendar"
                f" ({integrity}) must name the same Trading Calendar file"
            )


def default_config_path() -> Path:
    return Path(os.environ.get(CONFIG_PATH_VAR, DEFAULT_CONFIG_PATH))


# Sections that were removed, each with what replaced it, so an old file is told where
# its settings went rather than only that the section is unknown.
RETIRED_SECTIONS = {
    "reconcile": "[reconcile] is retired; Kite Parity's settings are in [parity]"
    " (close_tolerance, max_unexplained_pct) and the Integrity Check's"
    " in [integrity], ADR 0018",
}
# Keys that were removed from a section, each with what replaced it.
RETIRED_KEYS = {
    ("parity", "additive_min_bars"): "parity.additive_min_bars was removed (ADR 0018, #62):"
    " Kite's Adjustment explains a close only in a Kite Basis of at least 20 compared bars,"
    " with at most 5 percent of a stretch's closes off the replay; delete the key",
}


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file {path} does not exist")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    for section, message in RETIRED_SECTIONS.items():
        if section in raw:
            raise ConfigError(f"{path}: {message}")
    for (section, key), message in RETIRED_KEYS.items():
        table = raw.get(section)
        if isinstance(table, dict) and key in table:
            raise ConfigError(f"{path}: {message}")
    return _build(Config, raw, prefix="")


def _build[T](cls: type[T], raw: dict[str, Any], *, prefix: str) -> T:
    types: dict[str, type] = get_type_hints(cls)
    known = [f.name for f in fields(cls)]  # type: ignore[arg-type]
    for key in raw:
        if key not in known:
            raise ConfigError(f"unknown config key {prefix}{key}")
    values: dict[str, Any] = {}
    for name in known:
        if name not in raw:
            continue
        value = raw[name]
        expected = types[name]
        if dataclasses.is_dataclass(expected):
            if not isinstance(value, dict):
                raise ConfigError(f"config key {prefix}{name} must be a table")
            values[name] = _build(expected, value, prefix=f"{prefix}{name}.")
        elif get_origin(expected) is list:
            (item,) = get_args(expected)
            if not isinstance(value, list) or not all(
                isinstance(v, item) and not isinstance(v, bool) for v in value
            ):
                raise ConfigError(f"config key {prefix}{name} must be a list of {item.__name__}")
            values[name] = value
        elif get_origin(expected) is dict:
            values[name] = _string_table(value, prefix=f"{prefix}{name}", types=get_args(expected))
        elif get_origin(expected) is Literal:
            # A choice: the section refuses a value outside it by name (``__post_init__``).
            if not isinstance(value, str):
                raise ConfigError(
                    f"config key {prefix}{name} must be str, got {type(value).__name__}"
                )
            values[name] = value
        elif not isinstance(value, expected):
            raise ConfigError(
                f"config key {prefix}{name} must be {_type_name(expected)},"
                f" got {type(value).__name__}"
            )
        else:
            values[name] = value
    return cls(**values)


def _type_name(expected: Any) -> str:
    """A type as an error names it: ``float or false`` for a Limit that can be off."""
    if get_origin(expected) is types.UnionType:
        return " or ".join("false" if t is bool else t.__name__ for t in get_args(expected))
    return str(expected.__name__)


def _string_table(value: Any, *, prefix: str, types: tuple[Any, ...]) -> dict[str, str]:
    """A table of string values (the only kind of table that is not a section)."""
    if types != (str, str):
        raise TypeError(f"config key {prefix}: only tables of strings are supported")
    if not isinstance(value, dict):
        raise ConfigError(f"config key {prefix} must be a table")
    for key, item in value.items():
        if not isinstance(item, str):
            raise ConfigError(f"config key {prefix}.{key} must be str, got {type(item).__name__}")
    return dict(value)
