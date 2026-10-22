"""The ``tradeline`` console entry point.

Commands are thin: ``main`` parses arguments, configures JSON logging under the
command's name, loads configuration and secrets, and calls an application
service. Tests call the same services in-process.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from tradeline.cited_actions import load_cited_actions
from tradeline.collect import collect_daily as collect_daily_service
from tradeline.compare_adjusted import compare_adjusted as compare_adjusted_service
from tradeline.config import Config, default_config_path, load_config
from tradeline.dates import IST, month_end, month_of, next_month, parse_month
from tradeline.db import app_connection, connect
from tradeline.environment import Environment
from tradeline.ingest.announcements import ingest_announcements
from tradeline.ingest.candles import CurrentUniverse, ingest_candles
from tradeline.ingest.cited_actions import ingest_cited_actions
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.delistings import ingest_delistings
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.news import Feed, ingest_news
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.instruments_report import instruments_report
from tradeline.logging import configure_logging, log_context
from tradeline.login import login as login_service
from tradeline.manual_links import load_manual_links
from tradeline.migrations import PendingMigrations, apply_migrations
from tradeline.rebuild_instruments import rebuild_instruments as rebuild_instruments_service
from tradeline.reconcile import reconcile as reconcile_service
from tradeline.secrets import (
    DATABASE_URL,
    KITE_ACCESS_TOKEN,
    KITE_API_KEY,
    EnvSecrets,
    MissingSecret,
    SecretsProvider,
)
from tradeline.sectors import load_sectors
from tradeline.sources.kite import (
    INTERVAL_DAY,
    INTERVAL_MINUTE,
    KiteError,
    KiteSession,
    login_url,
)
from tradeline.transport import (
    PlaybackTransport,
    RecordingTransport,
    Transport,
    transport_for,
)
from tradeline.universe import compute_universe
from tradeline.verify_universe import verify_universe as verify_universe_service

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
    if args.source == "news":
        return _ingest_news(ctx, args)
    if args.source == "symbol-changes":
        return _ingest_symbol_changes(ctx, args)
    if args.source == "delistings":
        return _ingest_delistings(ctx, args)
    if args.source == "cited-actions":
        return _ingest_cited_actions(ctx)
    end = args.start if args.end is None else args.end
    with _scope(args.start, end, getattr(args, "symbols", None)):
        return _ingest_range(ctx, args, end)


def _ingest_range(ctx: Context, args: argparse.Namespace, end: date) -> int:
    if args.source == "eod":
        return _ingest_eod(ctx, args, end)
    if args.source == "corporate-actions":
        return _ingest_corporate_actions(ctx, args, end)
    if args.source == "announcements":
        return _ingest_announcements(ctx, args, end)
    return _ingest_candles(ctx, args, end)


def _ingest_eod(ctx: Context, args: argparse.Namespace, end: date) -> int:
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_eod(conn, transport, start=args.start, end=end)
    log.info("ingest eod finished", extra=result.summary())
    return 0


def _ingest_candles(ctx: Context, args: argparse.Namespace, end: date) -> int:
    session = KiteSession(
        _transport(ctx, args),
        api_key=ctx.secrets.require(KITE_API_KEY),
        access_token=ctx.secrets.require(KITE_ACCESS_TOKEN),
    )
    universe = None
    if args.interval == INTERVAL_MINUTE and args.symbols is None:
        # Minute history is kept for the current Universe under the configured rule.
        month = args.universe_month or month_of(datetime.now(IST).date())
        universe = CurrentUniverse(month=month, rule_version=ctx.config.universe.version)
    elif args.universe_month is not None:
        raise ValueError("--universe-month applies to minute candles without --symbol")
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_candles(
            conn,
            session,
            start=args.start,
            end=end,
            interval=args.interval,
            symbols=args.symbols,
            universe=universe,
        )
    log.info("ingest candles finished", extra={"interval": args.interval, **result.summary()})
    return 0


def _ingest_corporate_actions(ctx: Context, args: argparse.Namespace, end: date) -> int:
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_corporate_actions(
            conn, transport, start=args.start, end=end, symbols=args.symbols
        )
    log.info("ingest corporate-actions finished", extra=result.summary())
    return 0


def _ingest_announcements(ctx: Context, args: argparse.Namespace, end: date) -> int:
    # BSE's API answers only a browser's TLS handshake; NSE accepts it too.
    transport = _transport(ctx, args, browser_tls=True)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_announcements(conn, transport, start=args.start, end=end)
    log.info("ingest announcements finished", extra=result.summary())
    return 0


def _ingest_news(ctx: Context, args: argparse.Namespace) -> int:
    """Every configured feed, or the ones named; exits 1 if any feed failed."""
    feeds = _feeds(ctx, args.feeds)
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_news(conn, transport, feeds=feeds)
    log.log(
        logging.ERROR if result.failed_feeds else logging.INFO,
        "ingest news finished",
        extra=result.summary(),
    )
    return 1 if result.failed_feeds else 0


def _ingest_symbol_changes(ctx: Context, args: argparse.Namespace) -> int:
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_symbol_changes(conn, transport)
    log.info("ingest symbol-changes finished", extra=result.summary())
    return 0


def _ingest_delistings(ctx: Context, args: argparse.Namespace) -> int:
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_delistings(conn, transport)
    log.info("ingest delistings finished", extra=result.summary())
    return 0


def _ingest_cited_actions(ctx: Context) -> int:
    """The cited Corporate Actions file, checked and stored; exits 1 if any row was refused."""
    rows = load_cited_actions(Path(ctx.config.corporate_actions.cited))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_cited_actions(conn, rows)
    log.log(
        logging.ERROR if result.refused else logging.INFO,
        "ingest cited-actions finished",
        extra=result.summary(),
    )
    return 1 if result.refused else 0


def _feeds(ctx: Context, names: Sequence[str] | None = None) -> list[Feed]:
    """The configured feeds named, in that order; every configured feed by default."""
    configured = ctx.config.news.feeds
    names = list(configured) if names is None else names
    unknown = [name for name in names if name not in configured]
    if unknown:
        raise ValueError(f"news feeds not configured in [news.feeds]: {', '.join(unknown)}")
    return [Feed(name, configured[name]) for name in names]


def reconcile(ctx: Context, args: argparse.Namespace) -> int:
    """Compare candles with end-of-day rows; fail unless under the configured threshold.

    The summary is the last JSON line on stdout; the table goes to stderr.
    """
    end = args.start if args.end is None else args.end
    with _scope(args.start, end, args.symbols):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            report = reconcile_service(
                conn, ctx.config.reconcile, start=args.start, end=end, symbols=args.symbols
            )
        sys.stderr.write(report.text())
        log.log(
            logging.INFO if report.passed else logging.ERROR,
            "reconcile finished",
            extra={"report": report.summary()},
        )
    return 0 if report.passed else 1


def compare_adjusted(ctx: Context, args: argparse.Namespace) -> int:
    """Our adjusted series against Kite's stored candles; exit 1 unless every bar is equal.

    The table goes to stderr; the summary is the last JSON line on stdout.
    """
    as_of = datetime.now(IST).date() if args.as_of is None else args.as_of
    end = as_of if args.end is None else args.end
    with _scope(args.start, end, args.symbols):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            report = compare_adjusted_service(
                conn, symbols=args.symbols, start=args.start, end=end, as_of=as_of
            )
        sys.stderr.write(report.text())
        log.log(
            logging.INFO if report.passed else logging.ERROR,
            "compare-adjusted finished",
            extra={"report": report.summary()},
        )
    return 0 if report.passed else 1


@contextmanager
def _scope(start: date, end: date, symbols: Sequence[str] | None = None) -> Iterator[None]:
    """Every line the command logs inside carries its range, and its symbols when named.

    An ingest run inside binds its own source and range over these.
    """
    fields: dict[str, object] = {"range_start": start.isoformat(), "range_end": end.isoformat()}
    if symbols is not None:
        fields["symbols"] = list(symbols)
    with log_context(**fields):
        yield


def _transport(ctx: Context, args: argparse.Namespace, *, browser_tls: bool = False) -> Transport:
    """Live by default (refused under test); fixtures can be played back or recorded."""
    if args.play_fixtures is not None:
        return PlaybackTransport(args.play_fixtures)
    transport = transport_for(ctx.environment, browser_tls=browser_tls)
    if args.record_fixtures is not None:
        return RecordingTransport(transport, args.record_fixtures)
    return transport


def collect_daily(ctx: Context, args: argparse.Namespace) -> int:
    """Every source for one day (today in IST by default); exits 1 if any source failed.

    A failed source never stops the others, so the command is safe to run
    unattended: the exit code and the last JSON line say what went wrong, and
    the ingest run history has a row for every source.
    """
    day = datetime.now(IST).date() if args.day is None else args.day
    feeds = _feeds(ctx)

    def kite() -> KiteSession:
        return KiteSession(
            _transport(ctx, args),
            api_key=ctx.secrets.require(KITE_API_KEY),
            access_token=ctx.secrets.require(KITE_ACCESS_TOKEN),
        )

    with _scope(day, day):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            result = collect_daily_service(
                conn,
                day=day,
                rule=ctx.config.universe,
                feeds=feeds,
                transport=_transport(ctx, args),
                browser_transport=_transport(ctx, args, browser_tls=True),
                kite=kite,
                manual_links=Path(ctx.config.instruments.manual_links),
            )
        log.log(
            logging.INFO if result.succeeded else logging.ERROR,
            "collect-daily finished",
            extra=result.summary(),
        )
    return 0 if result.succeeded else 1


def universe(ctx: Context, args: argparse.Namespace) -> int:
    """The Universe for each month from ``--month`` to ``--to`` under the configured rule."""
    end = args.month if args.to is None else args.to
    if end < args.month:
        raise ValueError(f"month {end:%Y-%m} is before {args.month:%Y-%m}")
    with (
        _scope(args.month, month_end(end)),
        app_connection(ctx.secrets.require(DATABASE_URL)) as conn,
    ):
        month = args.month
        while month <= end:
            compute_universe(conn, ctx.config.universe, month=month)
            month = next_month(month)
    return 0


def sectors(ctx: Context, args: argparse.Namespace) -> int:
    """Load NSE's current sector classification, in force from today (IST)."""
    transport = _transport(ctx, args)
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = load_sectors(conn, transport, effective_date=datetime.now(IST).date())
    log.info("sectors finished", extra=result.summary())
    return 0


def rebuild_instruments(ctx: Context, args: argparse.Namespace) -> int:
    """Rebuild the Instruments from the stored evidence and the manual links file,
    replacing the previous build."""
    manual_links = load_manual_links(Path(ctx.config.instruments.manual_links))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = rebuild_instruments_service(conn, manual_links=manual_links)
    log.info("rebuild-instruments finished", extra=result.summary())
    return 0


def instruments(ctx: Context, args: argparse.Namespace) -> int:
    """The anomalies, ended Instruments, Link Candidates, and stale Universe months; read only.

    The report goes to stderr; its summary is the last JSON line on stdout.
    """
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        report = instruments_report(conn)
    sys.stderr.write(report.text())
    log.info("instruments finished", extra={"report": report.summary()})
    return 0


def verify_universe(ctx: Context, args: argparse.Namespace) -> int:
    """Compare the month's Universe with NSE's current NIFTY 200; a report, so exit 0.

    The report goes to stderr; its summary is the last JSON line on stdout.
    """
    today = datetime.now(IST).date()
    month = month_of(today) if args.month is None else args.month
    transport = _transport(ctx, args)
    with _scope(month, month_end(month)):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            check = verify_universe_service(
                conn, transport, ctx.config.universe, month=month, as_of=today
            )
        sys.stderr.write(check.text())
        log.info("verify-universe finished", extra={"report": check.summary()})
    return 0


Command = Callable[[Context, argparse.Namespace], int]

COMMANDS: dict[str, Command] = {
    "migrate": migrate,
    "login": login,
    "ingest": ingest,
    "universe": universe,
    "reconcile": reconcile,
    "compare-adjusted": compare_adjusted,
    "sectors": sectors,
    "rebuild-instruments": rebuild_instruments,
    "instruments": instruments,
    "verify-universe": verify_universe,
    "collect-daily": collect_daily,
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
        help="Kite's candles for symbols over a date range (needs `login` today)",
    )
    candles.add_argument(
        "--interval",
        choices=[INTERVAL_DAY, INTERVAL_MINUTE],
        required=True,
        help="candle interval",
    )
    candles.add_argument(
        "--symbol",
        dest="symbols",
        metavar="SYMBOL",
        action="append",
        help="an NSE symbol to load under every series it has; repeatable;"
        " defaults to every EQ series symbol with a Kite token for day candles,"
        " and to the current Universe for minute candles",
    )
    candles.add_argument(
        "--universe-month",
        metavar="YYYY-MM",
        type=parse_month,
        help="minute candles: the Universe month whose members are loaded, under the"
        " configured rule; defaults to the current month in India",
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
    sources.add_parser(
        "cited-actions",
        help="the splits and bonuses of the cited corporate actions file, checked against the"
        " price; exits 1 if any row is refused",
    )
    sources.add_parser(
        "announcements",
        parents=[fixtures, date_range],
        help="NSE and BSE corporate announcements for each day in the range, matched to symbols",
    )
    sources.add_parser(
        "symbol-changes",
        parents=[fixtures],
        help="NSE's symbol-change list as it stands today, stored as published",
    )
    sources.add_parser(
        "delistings",
        parents=[fixtures],
        help="NSE's list of delisted securities as it stands today, stored as published",
    )
    news = sources.add_parser(
        "news",
        parents=[fixtures],
        help="the stories the configured RSS feeds carry now, de-duplicated and matched",
    )
    news.add_argument(
        "--feed",
        dest="feeds",
        metavar="NAME",
        action="append",
        help="a feed named in [news.feeds] to load; repeatable; defaults to every feed",
    )

    universe_parser = subparsers.add_parser(
        "universe",
        help="compute and record the Universe for months from loaded end-of-day files",
    )
    universe_parser.add_argument(
        "--month", metavar="YYYY-MM", type=parse_month, required=True, help="the first month"
    )
    universe_parser.add_argument(
        "--to",
        metavar="YYYY-MM",
        type=parse_month,
        help="the last month; defaults to --month",
    )

    subparsers.add_parser(
        "sectors",
        parents=[fixtures],
        help="load NSE's sector classification, in force from today; changes add dated rows",
    )
    subparsers.add_parser(
        "rebuild-instruments",
        help="rebuild the Instruments from the stored end-of-day rows, symbol changes,"
        " corporate actions, and listing dates, and the manual links file",
    )
    subparsers.add_parser(
        "instruments",
        help="review the last rebuild: anomalies, ended Instruments, Link Candidates, and"
        " stale Universe months; writes nothing",
    )
    verify_parser = subparsers.add_parser(
        "verify-universe",
        parents=[fixtures],
        help="report the Universe's overlap with NSE's current NIFTY 200, and members without"
        " a sector",
    )
    verify_parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        type=parse_month,
        help="the Universe month to compare; defaults to the current month",
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        parents=[date_range],
        help="compare daily candles with NSE end-of-day files; exit 1 unless under the threshold",
    )
    reconcile_parser.add_argument(
        "--symbol",
        dest="symbols",
        metavar="SYMBOL",
        action="append",
        help="an NSE symbol to compare; repeatable; defaults to every symbol with candles",
    )
    compare_parser = subparsers.add_parser(
        "compare-adjusted",
        help="compare our adjusted series with Kite's stored candles bar by bar;"
        " exit 1 unless every bar is equal",
    )
    compare_parser.add_argument(
        "--from", dest="start", metavar="DATE", type=date.fromisoformat, required=True
    )
    compare_parser.add_argument(
        "--to", dest="end", metavar="DATE", type=date.fromisoformat, help="defaults to --as-of"
    )
    compare_parser.add_argument(
        "--as-of",
        metavar="DATE",
        type=date.fromisoformat,
        help="the adjustment date, normally the day the candles were fetched; defaults to today",
    )
    compare_parser.add_argument(
        "--symbol",
        dest="symbols",
        metavar="SYMBOL",
        action="append",
        required=True,
        help="an NSE symbol to compare; repeatable",
    )
    collect_parser = subparsers.add_parser(
        "collect-daily",
        parents=[fixtures],
        help="the day's end-of-day file, Universe candles, announcements, and news;"
        " exit 1 if any source failed (needs `login` today for the candles)",
    )
    collect_parser.add_argument(
        "--date",
        dest="day",
        metavar="DATE",
        type=date.fromisoformat,
        help="the trading day to collect; defaults to today in IST",
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
    except (
        MissingSecret,
        KiteError,
        PendingMigrations,
        ValueError,  # ConfigError is a ValueError
    ) as error:
        log.error(str(error))
        return 1
    except Exception:
        log.exception("command failed")
        return 1
