"""Secrets providers.

Secrets never live in the repository or the database. In development they come
from a gitignored ``.env`` at the repo root (process environment overriding);
in production from the process environment (``EnvSecrets``) or, from Phase 6,
SSM Parameter Store (``SsmSecrets``). The daily Kite access token obtained by
``login`` is written back through the same provider.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path
from typing import Protocol

from tradeline.environment import Environment

KITE_API_KEY = "KITE_API_KEY"
KITE_API_SECRET = "KITE_API_SECRET"
KITE_REDIRECT_URL = "KITE_REDIRECT_URL"
KITE_ACCESS_TOKEN = "KITE_ACCESS_TOKEN"
KITE_ACCESS_TOKEN_ISSUED_ON = "KITE_ACCESS_TOKEN_ISSUED_ON"
DATABASE_URL = "DATABASE_URL"

DEFAULT_KITE_REDIRECT_URL = "http://127.0.0.1:8765/callback"
DEFAULT_ENV_FILE = Path(".env")


class MissingSecret(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name} is not set")
        self.name = name


class SecretsProvider(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...

    def require(self, name: str) -> str: ...

    def put(self, name: str, value: str) -> None: ...


class EnvSecrets:
    """Secrets from the process environment, plus a ``.env`` file in development.

    ``put`` writes the ``.env`` file in development so a value (the daily access
    token) survives the process; elsewhere it is visible to this process only.
    """

    def __init__(
        self,
        *,
        environ: MutableMapping[str, str],
        env_file: Path | None,
    ) -> None:
        self._environ = environ
        self._env_file = env_file
        self._file_values: dict[str, str] = (
            _parse_env_file(env_file) if env_file is not None and env_file.exists() else {}
        )

    @classmethod
    def for_environment(
        cls,
        environment: Environment,
        *,
        env_file: Path = DEFAULT_ENV_FILE,
        environ: MutableMapping[str, str] | None = None,
    ) -> EnvSecrets:
        return cls(
            environ=os.environ if environ is None else environ,
            env_file=env_file if environment is Environment.DEVELOPMENT else None,
        )

    def get(self, name: str, default: str | None = None) -> str | None:
        value = self._environ.get(name)
        if value is None:
            value = self._file_values.get(name)
        return default if value is None else value

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise MissingSecret(name)
        return value

    def put(self, name: str, value: str) -> None:
        if self._env_file is None:
            self._environ[name] = value
            return
        self._file_values[name] = value
        _write_env_file(self._env_file, name, value)


class SsmSecrets:
    """AWS SSM Parameter Store provider. Interface only; implemented in Phase 6."""

    def __init__(self, *, parameter_path: str) -> None:
        self.parameter_path = parameter_path

    def get(self, name: str, default: str | None = None) -> str | None:
        raise NotImplementedError("SSM secrets provider arrives in Phase 6")

    def require(self, name: str) -> str:
        raise NotImplementedError("SSM secrets provider arrives in Phase 6")

    def put(self, name: str, value: str) -> None:
        raise NotImplementedError("SSM secrets provider arrives in Phase 6")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def _parse_env_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    line = line.removeprefix("export ").lstrip()
    name, _, value = line.partition("=")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return name.strip(), value


def _write_env_file(path: Path, name: str, value: str) -> None:
    """Replace the line for ``name`` in place, or append one; other lines are untouched."""
    lines = path.read_text().splitlines() if path.exists() else []
    new_line = f"{name}={value}"
    replaced = False
    for i, line in enumerate(lines):
        parsed = _parse_env_line(line)
        if parsed is not None and parsed[0] == name:
            lines[i] = new_line
            replaced = True
    if not replaced:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n")
