"""Which environment the process runs in, read from ``TRADELINE_ENV``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import Enum

ENV_VAR = "TRADELINE_ENV"


class Environment(Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Environment:
        raw = environ.get(ENV_VAR, cls.DEVELOPMENT.value)
        try:
            return cls(raw)
        except ValueError:
            allowed = ", ".join(e.value for e in cls)
            raise ValueError(f"{ENV_VAR}={raw!r} is not one of: {allowed}") from None

    @classmethod
    def current(cls) -> Environment:
        return cls.from_environ(os.environ)
