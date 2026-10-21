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
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

CONFIG_PATH_VAR = "TRADELINE_CONFIG"
DEFAULT_CONFIG_PATH = Path("tradeline.toml")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UniverseRule:
    """The Universe Rule's parameters (ADR 0005); see ``tradeline.universe``.

    Every field takes part in ``version``, so any change to the rule is a new
    rule version and the membership it produces is recorded under it.
    """

    top_n: int = 200
    # The trailing window, in trading days before the month's first trading day.
    lookback_trading_days: int = 126
    # Trading days from the listing date to the month's first trading day.
    min_listing_trading_days: int = 126
    # Rows required in the window, as a whole percentage of its trading days.
    min_presence_percent: int = 90
    # The only series eligible: EQ excludes SME (SM, ST) and trade-for-trade (BE, BZ).
    series: str = "EQ"
    # ETFs and other fund units trade in series EQ; their ISINs begin with INF.
    excluded_isin_prefix: str = "INF"

    def __post_init__(self) -> None:
        for name in ("top_n", "lookback_trading_days", "min_listing_trading_days"):
            if getattr(self, name) < 1:
                raise ConfigError(f"universe.{name} must be at least 1")
        if not 0 < self.min_presence_percent <= 100:
            raise ConfigError("universe.min_presence_percent must be between 1 and 100")

    @property
    def parameters(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

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
    class Reconcile:
        # A candle's close differs from the end-of-day close when it is further
        # from it, once Kite's own adjustment factor is taken out, than this
        # fraction of the price plus half of Kite's 5 paisa tick.
        close_tolerance: float = 0.001
        # ``reconcile`` fails when missing, extra, and mismatched rows together
        # exceed this percentage of the rows compared (Phase 1: under 0.1).
        max_discrepancy_pct: float = 0.1

    logging: Logging = field(default_factory=Logging)
    universe: UniverseRule = field(default_factory=UniverseRule)
    reconcile: Reconcile = field(default_factory=Reconcile)
    news: News = field(default_factory=News)


def default_config_path() -> Path:
    return Path(os.environ.get(CONFIG_PATH_VAR, DEFAULT_CONFIG_PATH))


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file {path} does not exist")
    with path.open("rb") as f:
        raw = tomllib.load(f)
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
        elif get_origin(expected) is dict:
            values[name] = _string_table(value, prefix=f"{prefix}{name}", types=get_args(expected))
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
