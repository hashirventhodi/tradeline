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
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

from tradeline.costs import Slippage
from tradeline.dates import parse_month
from tradeline.protocol import Gate, Layout, Terms
from tradeline.risk import Limits

CONFIG_PATH_VAR = "TRADELINE_CONFIG"
DEFAULT_CONFIG_PATH = Path("tradeline.toml")


class ConfigError(ValueError):
    pass


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

    @dataclass(frozen=True)
    class Risk:
        """The Limits of ``docs/risk-controls.md`` the Risk Engine enforces (``tradeline.risk``),
        fractions of Equity where they are, and the tick table Plans are priced on."""

        risk_per_trade: float = 0.01
        max_open_positions: int = 8
        max_position: float = 0.15
        max_sector: float = 0.30
        max_gross: float = 1.0
        # Of the 20-day average daily traded value.
        participation_cap: float = 0.01
        # Rupees.
        min_order_value: int = 5000
        max_new_entries: int = 4
        min_reward_to_risk: float = 1.5
        # The checked-in dated table of NSE tick sizes (``tradeline.tick_sizes``),
        # relative to the working directory.
        tick_sizes: str = "tick_sizes.csv"

        def limits(self) -> Limits:
            return Limits(
                risk_per_trade=Decimal(str(self.risk_per_trade)),
                max_open_positions=self.max_open_positions,
                max_position=Decimal(str(self.max_position)),
                max_sector=Decimal(str(self.max_sector)),
                max_gross=Decimal(str(self.max_gross)),
                participation_cap=Decimal(str(self.participation_cap)),
                min_order_value=Decimal(self.min_order_value),
                max_new_entries=self.max_new_entries,
                min_reward_to_risk=Decimal(str(self.min_reward_to_risk)),
            )

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
    integrity: Integrity = field(default_factory=Integrity)
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    protocol: Protocol = field(default_factory=Protocol)

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
                f"config key {prefix}{name} must be {expected.__name__}, got {type(value).__name__}"
            )
        else:
            values[name] = value
    return cls(**values)


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
