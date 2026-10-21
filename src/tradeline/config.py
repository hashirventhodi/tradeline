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
from typing import Any, get_type_hints

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

    logging: Logging = field(default_factory=Logging)
    universe: UniverseRule = field(default_factory=UniverseRule)


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
        elif not isinstance(value, expected):
            raise ConfigError(
                f"config key {prefix}{name} must be {expected.__name__}, got {type(value).__name__}"
            )
        else:
            values[name] = value
    return cls(**values)
