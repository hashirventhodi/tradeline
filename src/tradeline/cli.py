"""The ``tradeline`` console entry point.

Commands are thin: ``main`` parses arguments, configures JSON logging under the
command's name, loads configuration and secrets, and calls an application
service. Tests call the same services in-process.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tradeline.config import Config, default_config_path, load_config
from tradeline.db import app_connection, connect
from tradeline.environment import Environment
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.logging import configure_logging
from tradeline.login import login as login_service
from tradeline.migrations import apply_migrations
from tradeline.secrets import (
    DATABASE_URL,
    KITE_ACCESS_TOKEN,
    KITE_API_KEY,
    EnvSecrets,
    MissingSecret,
    SecretsProvider,
)
from tradeline.sources.kite import INTERVAL_DAY, KiteError, KiteSession, login_url
from tradeline.transport import (
    PlaybackTransport,
    RecordingTransport,
    Transport,
    transport_for,
)
from tradeline.universe import compute_universe

log = logging.getLogger(__name__)

# Kite's access tokens are issued and expire on the Indian calendar day.
IST = ZoneInfo("Asia/Kolkata")


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


def login(ctx: Context, args: argparse.Namespace) -> int:
    """Print the Kite login URL, take the request token back, store the day's access token.

    The URL and the prompt go to stderr so stdout stays one JSON line per event;
    the URL carries the API key, so it is not logged.
    """
    url = login_url(ctx.secrets.require(KITE_API_KEY))
    log.info("kite login url printed; waiting for the request token")
    sys.stderr.write(f"Open this URL, log in to Kite, and paste back the request_token:\n{url}\n")
    request_token = args.request_token
    if request_token is None:
        sys.stderr.write("request_token: ")
        sys.stderr.flush()
        request_token = sys.stdin.readline().strip()
    if not request_token:
        raise ValueError("no request token was given")
    # Playback only: a recording would write the API key and a live request
    # token into a fixture body, which the transport's rule against recording
    # credentials exists to prevent.
    transport = (
        PlaybackTransport(args.play_fixtures)
        if args.play_fixtures is not None
        else transport_for(ctx.environment)
    )
    result = login_service(
        ctx.secrets, transport, request_token=request_token, today=datetime.now(IST).date()
    )
    log.info("login finished", extra={"user_id": result.user_id, "issued_on": result.issued_on})
    return 0


def ingest(ctx: Context, args: argparse.Namespace) -> int:
    end = args.start if args.end is None else args.end
    if args.source == "eod":
        return _ingest_eod(ctx, args, end)
    if args.source == "corporate-actions":
        return _ingest_corporate_actions(ctx, args, end)
    return _ingest_candles(ctx, args, end)


def _ingest_eod(ctx: Context, args: argparse.Namespace, end: date) -> int:
    transport = _transport(ctx, args)
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


def _ingest_candles(ctx: Context, args: argparse.Namespace, end: date) -> int:
    session = KiteSession(
        _transport(ctx, args),
        api_key=ctx.secrets.require(KITE_API_KEY),
        access_token=ctx.secrets.require(KITE_ACCESS_TOKEN),
    )
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_candles(conn, session, start=args.start, end=end, symbols=args.symbols)
    log.info(
        "ingest candles finished",
        extra={
            "interval": args.interval,
            "symbols_loaded": result.symbols_loaded,
            "symbols_without_token": list(result.symbols_without_token),
            "rows_written": result.rows_written,
            "anomalies": result.anomalies,
            "requests": result.requests,
        },
    )
    return 0


def _ingest_corporate_actions(ctx: Context, args: argparse.Namespace, end: date) -> int:
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_corporate_actions(
            conn, transport, start=args.start, end=end, symbols=args.symbols
        )
    log.info(
        "ingest corporate-actions finished",
        extra={
            "rows_seen": result.rows_seen,
            "rows_written": result.rows_written,
            "unparsed": result.unparsed,
            "requests": result.requests,
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


def universe(ctx: Context, args: argparse.Namespace) -> int:
    """The Universe for each month from ``--month`` to ``--to`` under the configured rule."""
    end = args.month if args.to is None else args.to
    if end < args.month:
        raise ValueError(f"month {end:%Y-%m} is before {args.month:%Y-%m}")
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        month = args.month
        while month <= end:
            compute_universe(conn, ctx.config.universe, month=month)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return 0


def _month(text: str) -> date:
    """A month as YYYY-MM, as its first day."""
    return datetime.strptime(text, "%Y-%m").date()


Command = Callable[[Context, argparse.Namespace], int]

COMMANDS: dict[str, Command] = {
    "migrate": migrate,
    "login": login,
    "ingest": ingest,
    "universe": universe,
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
    date_range = argparse.ArgumentParser(add_help=False)
    date_range.add_argument(
        "--from", dest="start", metavar="DATE", type=date.fromisoformat, required=True
    )
    date_range.add_argument(
        "--to",
        dest="end",
        metavar="DATE",
        type=date.fromisoformat,
        help="inclusive; defaults to --from",
    )

    login_parser = subparsers.add_parser(
        "login",
        help="the daily Kite Session Login: print the login URL, store the access token",
    )
    login_parser.add_argument(
        "--play-fixtures",
        metavar="DIR",
        type=Path,
        help="serve a recorded token exchange from DIR instead of calling Kite",
    )
    login_parser.add_argument(
        "--request-token",
        metavar="TOKEN",
        help="the request_token from the login redirect; prompted for when omitted",
    )

    ingest_parser = subparsers.add_parser("ingest", help="load a source into the database")
    sources = ingest_parser.add_subparsers(dest="source", required=True)
    sources.add_parser(
        "eod",
        parents=[fixtures, date_range],
        help="NSE end-of-day files for a date range, plus the instrument master",
    )
    candles = sources.add_parser(
        "candles",
        parents=[fixtures, date_range],
        help="raw Kite candles for symbols over a date range (needs `login` today)",
    )
    candles.add_argument(
        "--interval",
        choices=[INTERVAL_DAY],
        required=True,
        help="candle interval; minute candles arrive in a later issue",
    )
    candles.add_argument(
        "--symbol",
        dest="symbols",
        metavar="SYMBOL",
        action="append",
        help="an NSE symbol to load under every series it has; repeatable;"
        " defaults to every EQ series symbol with a Kite token",
    )
    actions = sources.add_parser(
        "corporate-actions",
        parents=[fixtures, date_range],
        help="NSE corporate actions with an ex-date in the range, with our adjustment factors",
    )
    actions.add_argument(
        "--symbol",
        dest="symbols",
        metavar="SYMBOL",
        action="append",
        help="an NSE symbol to load; repeatable; defaults to every symbol NSE lists",
    )

    universe_parser = subparsers.add_parser(
        "universe",
        help="compute and record the Universe for months from loaded end-of-day files",
    )
    universe_parser.add_argument(
        "--month", metavar="YYYY-MM", type=_month, required=True, help="the first month"
    )
    universe_parser.add_argument(
        "--to",
        metavar="YYYY-MM",
        type=_month,
        help="the last month; defaults to --month",
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
    except (MissingSecret, KiteError, ValueError) as error:  # ConfigError is a ValueError
        log.error(str(error))
        return 1
    except Exception:
        log.exception("command failed")
        return 1
