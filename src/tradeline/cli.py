"""The ``tradeline`` console entry point.

Commands are thin: ``main`` parses arguments, configures JSON logging under the
command's name, loads configuration and secrets, and calls an application
service. Tests call the same services in-process.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tradeline.config import Config, default_config_path, load_config
from tradeline.db import app_connection, connect
from tradeline.environment import Environment
from tradeline.ingest.eod import ingest_eod
from tradeline.logging import configure_logging
from tradeline.migrations import apply_migrations
from tradeline.secrets import DATABASE_URL, EnvSecrets, MissingSecret, SecretsProvider
from tradeline.transport import (
    PlaybackTransport,
    RecordingTransport,
    Transport,
    transport_for,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Context:
    environment: Environment
    config: Config
    secrets: SecretsProvider


def migrate(ctx: Context, args: argparse.Namespace) -> int:
    with connect(ctx.secrets.require(DATABASE_URL)) as conn:
        applied = apply_migrations(conn)
    log.info("migrations up to date", extra={"applied": len(applied)})
    return 0


def ingest(ctx: Context, args: argparse.Namespace) -> int:
    transport = _transport(ctx, args)
    end = args.start if args.end is None else args.end
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_eod(conn, transport, start=args.start, end=end)
    log.info(
        "ingest eod finished",
        extra={
            "days_loaded": result.days_loaded,
            "missing_days": [day.isoformat() for day in result.missing_days],
            "eod_rows_written": result.eod_rows_written,
            "anomalies": result.anomalies,
            "symbol_updates": result.symbol_updates,
        },
    )
    return 0


def _transport(ctx: Context, args: argparse.Namespace) -> Transport:
    """Live by default (refused under test); fixtures can be played back or recorded."""
    if args.play_fixtures is not None:
        return PlaybackTransport(args.play_fixtures)
    transport = transport_for(ctx.environment)
    if args.record_fixtures is not None:
        return RecordingTransport(transport, args.record_fixtures)
    return transport


Command = Callable[[Context, argparse.Namespace], int]

COMMANDS: dict[str, Command] = {
    "migrate": migrate,
    "ingest": ingest,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="apply pending schema migrations to DATABASE_URL")

    fixtures = argparse.ArgumentParser(add_help=False)
    fixtures.add_argument(
        "--play-fixtures",
        metavar="DIR",
        type=Path,
        help="serve recorded fixtures from DIR instead of calling the network",
    )
    fixtures.add_argument(
        "--record-fixtures",
        metavar="DIR",
        type=Path,
        help="record every live response into DIR as a fixture",
    )
    ingest_parser = subparsers.add_parser("ingest", help="load a source into the database")
    sources = ingest_parser.add_subparsers(dest="source", required=True)
    eod = sources.add_parser(
        "eod",
        parents=[fixtures],
        help="NSE end-of-day files for a date range, plus the instrument master",
    )
    eod.add_argument("--from", dest="start", metavar="DATE", type=date.fromisoformat, required=True)
    eod.add_argument(
        "--to",
        dest="end",
        metavar="DATE",
        type=date.fromisoformat,
        help="inclusive; defaults to --from",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Logging is configured before anything can fail so even a bad config file
    # is reported as a JSON line; the configured level is applied once known.
    configure_logging(command=args.command, level="INFO")
    try:
        environment = Environment.current()
        config = load_config(default_config_path())
        configure_logging(command=args.command, level=config.logging.level)
        ctx = Context(
            environment=environment,
            config=config,
            secrets=EnvSecrets.for_environment(environment),
        )
        return COMMANDS[args.command](ctx, args)
    except (MissingSecret, ValueError) as error:  # ConfigError is a ValueError
        log.error(str(error))
        return 1
    except Exception:
        log.exception("command failed")
        return 1
