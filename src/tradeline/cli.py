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

from tradeline.config import Config, ConfigError, default_config_path, load_config
from tradeline.db import connect
from tradeline.environment import Environment
from tradeline.logging import configure_logging
from tradeline.migrations import apply_migrations
from tradeline.secrets import DATABASE_URL, EnvSecrets, MissingSecret, SecretsProvider

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


Command = Callable[[Context, argparse.Namespace], int]

COMMANDS: dict[str, Command] = {
    "migrate": migrate,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="apply pending schema migrations to DATABASE_URL")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    except (ConfigError, MissingSecret, ValueError) as error:
        log.error(str(error))
        return 1
    except Exception:
        log.exception("command failed")
        return 1
