"""Structured JSON logging to stdout.

Every line is one JSON object carrying the command name; ``log_context`` binds
extra fields (source, symbol, run id) for everything logged inside it. Phase 6
collects these lines as they are.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, TextIO

_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "tradeline_log_context", default=MappingProxyType({})
)

# Attributes every LogRecord has; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, *, command: str) -> None:
        super().__init__()
        self.command = command

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "command": self.command,
            "msg": record.getMessage(),
        }
        line.update(_context.get())
        line.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            line["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(*, command: str, level: str, stream: TextIO | None = None) -> None:
    """Replace the root handler so every log line is JSON tagged with ``command``."""
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(JsonFormatter(command=command))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)
