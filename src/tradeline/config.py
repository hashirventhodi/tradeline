"""Non-secret settings, loaded from ``tradeline.toml``.

Each section is a frozen dataclass with defaults; unknown sections, unknown
keys, and wrong types are refused by name so a typo cannot silently fall back
to a default. Secrets never belong here (see ``tradeline.secrets``).
"""

from __future__ import annotations

import dataclasses
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
class Config:
    @dataclass(frozen=True)
    class Logging:
        level: str = "INFO"

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
    reconcile: Reconcile = field(default_factory=Reconcile)


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
