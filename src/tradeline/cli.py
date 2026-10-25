"""The ``tradeline`` console entry point.

Commands are thin: ``main`` parses arguments, configures JSON logging under the
command's name, loads configuration and secrets, and calls an application
service. Tests call the same services in-process.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from tradeline import queries, revision
from tradeline import universe as universe_service
from tradeline.benchmark import load_benchmark_funds
from tradeline.cited_actions import load_cited_actions
from tradeline.collect import collect_daily as collect_daily_service
from tradeline.compare_adjusted import compare_adjusted as compare_adjusted_service
from tradeline.config import Config, default_config_path, load_config
from tradeline.conformance import check_conformance
from tradeline.costs import CostModel, load_contract_notes, load_cost_schedule
from tradeline.dates import IST, month_end, month_of, next_month, parse_month
from tradeline.db import NotAppRoleMember, app_connection, connect
from tradeline.demergers import load_demergers
from tradeline.engine import WallClock
from tradeline.engine import backtest as backtest_service
from tradeline.engine import paper as paper_service
from tradeline.environment import Environment
from tradeline.freeze import freeze_protocol
from tradeline.holdout import run_fresh_period, run_holdout
from tradeline.ingest.announcements import ingest_announcements
from tradeline.ingest.benchmark import ingest_benchmark
from tradeline.ingest.candles import CurrentUniverse, ingest_candles
from tradeline.ingest.cited_actions import ingest_cited_actions
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.delistings import ingest_delistings
from tradeline.ingest.demergers import ingest_demergers
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.news import Feed, ingest_news
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.instruments_report import instruments_report
from tradeline.integrity import integrity as integrity_service
from tradeline.logging import configure_logging, log_context
from tradeline.login import login as login_service
from tradeline.manual_links import load_manual_links
from tradeline.migrations import PendingMigrations, UnreadableMigrations, apply_migrations
from tradeline.parity import parity as parity_service
from tradeline.protocol import Terms, window_boundaries, windows_text
from tradeline.protocol_report import report_protocol
from tradeline.rebuild_instruments import rebuild_instruments as rebuild_instruments_service
from tradeline.report import report_backtest
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
from tradeline.strategy.lowvol import LowvolV1
from tradeline.strategy.portfolio import AnyStrategy
from tradeline.strategy.pullback import PullbackV1
from tradeline.tax import load_tax_schedule
from tradeline.tick_sizes import load_tick_sizes
from tradeline.trading_calendar import load_trading_calendar
from tradeline.transport import (
    PlaybackTransport,
    RecordingTransport,
    Transport,
    transport_for,
)
from tradeline.verify_universe import verify_universe as verify_universe_service
from tradeline.walk_forward import StrategyFactory
from tradeline.walk_forward import walk_forward as walk_forward_service
from tradeline.weekly_integrity import frozen_on, weekly_integrity

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
    if args.source == "demergers":
        return _ingest_demergers(ctx)
    if args.source == "benchmark":
        return _ingest_benchmark(ctx, args)
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


def _ingest_demergers(ctx: Context) -> int:
    """The demerger table, read whole and stored as the table in force."""
    table = load_demergers(Path(ctx.config.corporate_actions.demergers))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_demergers(conn, table)
    log.info("ingest demergers finished", extra=result.summary())
    return 0


def _ingest_benchmark(ctx: Context, args: argparse.Namespace) -> int:
    """The Benchmark's and the factor fund's AMFI scheme lines and NAVs to ``--to`` (today in
    India by default); played-back fixtures are not spaced out as live calls are."""
    end = datetime.now(IST).date() if args.end is None else args.end
    funds = load_benchmark_funds(Path(ctx.config.benchmark.funds))
    transport = _transport(ctx, args)
    sleep = (lambda _seconds: None) if args.play_fixtures is not None else time.sleep
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = ingest_benchmark(
            conn,
            transport,
            funds=funds,
            factor_fund=ctx.config.benchmark.factor_fund,
            end=end,
            sleep=sleep,
        )
    log.info("ingest benchmark finished", extra=result.summary())
    return 0


def _feeds(ctx: Context, names: Sequence[str] | None = None) -> list[Feed]:
    """The configured feeds named, in that order; every configured feed by default."""
    configured = ctx.config.news.feeds
    names = list(configured) if names is None else names
    unknown = [name for name in names if name not in configured]
    if unknown:
        raise ValueError(f"news feeds not configured in [news.feeds]: {', '.join(unknown)}")
    return [Feed(name, configured[name]) for name in names]


def parity(ctx: Context, args: argparse.Namespace) -> int:
    """Kite Parity: fail unless unexplained differences are under the configured threshold.

    The summary is the last JSON line on stdout; the table goes to stderr.
    """
    end = args.start if args.end is None else args.end
    with _scope(args.start, end, args.symbols):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            report = parity_service(
                conn, ctx.config.parity, start=args.start, end=end, symbols=args.symbols
            )
        sys.stderr.write(report.text())
        log.log(
            logging.INFO if report.passed else logging.ERROR,
            "parity finished",
            extra={"report": report.summary()},
        )
    return 0 if report.passed else 1


def compare_adjusted(ctx: Context, args: argparse.Namespace) -> int:
    """Factor Agreement, and named Symbols' bars against Kite's; exit 1 if an Ex-Date disagrees.

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
            universe_service.compute_universe(conn, ctx.config.universe, month=month)
            month = next_month(month)
    return 0


def declare_rule_validity(ctx: Context, args: argparse.Namespace) -> int:
    """Declare a Universe Rule version, the configured one by default, in force from a date."""
    version = ctx.config.universe.version if args.rule_version is None else args.rule_version
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        universe_service.declare_rule_validity(
            conn,
            version,
            in_force_from=args.in_force_from,
            declared_at=datetime.now(IST),
            rule=ctx.config.universe,
        )
    return 0


def backfill_universe_builds(ctx: Context, args: argparse.Namespace) -> int:
    """Give every stored Universe month without a build one, and list where each cutoff came
    from."""
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = universe_service.backfill_universe_builds(conn)
    log.info("backfill-universe-builds finished", extra=result.summary())
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
    """The anomalies, superseded Cited Actions, ended Instruments, Link Candidates, and stale
    Universe months; read only.

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


def check_universe(ctx: Context, args: argparse.Namespace) -> int:
    """The Conformance Check of each stored month from ``--month`` to ``--to`` under the
    configured rule; exit 1 unless every month conforms.

    The summary is the last JSON line on stdout; the report goes to stderr.
    Nothing is written.
    """
    end = args.month if args.to is None else args.to
    if end < args.month:
        raise ValueError(f"month {end:%Y-%m} is before {args.month:%Y-%m}")
    months = []
    month = args.month
    while month <= end:
        months.append(month)
        month = next_month(month)
    with _scope(args.month, month_end(end)):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            report = check_conformance(conn, ctx.config.universe, months=months)
        sys.stderr.write(report.text())
        log.log(
            logging.INFO if report.passed else logging.ERROR,
            "check-universe finished",
            extra={"report": report.summary()},
        )
    return 0 if report.passed else 1


def integrity(ctx: Context, args: argparse.Namespace) -> int:
    """The Integrity Check of NSE's data against NSE alone; exit 1 unless it passes.

    The summary is the last JSON line on stdout; the table goes to stderr.
    Nothing is written.
    """
    end = args.start if args.end is None else args.end
    with _scope(args.start, end):
        with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
            report = integrity_service(conn, ctx.config.integrity, start=args.start, end=end)
        sys.stderr.write(report.text())
        log.log(
            logging.INFO if report.passed else logging.ERROR,
            "integrity finished",
            extra={"report": report.summary()},
        )
    return 0 if report.passed else 1


def integrity_weekly(ctx: Context, args: argparse.Namespace) -> int:
    """The weekly Integrity Check over every day after the freeze of the Protocol
    ``--protocol`` names (or after ``--after``) to ``--to`` (today in IST by default), with
    its Kite Parity gaps; exit 1 and log an ``integrity alert`` at ERROR if it fails.

    The summary is the last JSON line on stdout; the table goes to stderr. Nothing is
    written.
    """
    end = datetime.now(IST).date() if args.end is None else args.end
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        after = args.after if args.protocol is None else frozen_on(conn, args.protocol)
        with _scope(after + timedelta(days=1), end):
            report = weekly_integrity(
                conn, ctx.config.integrity, ctx.config.parity, after=after, end=end
            )
    sys.stderr.write(report.text())
    if report.passed:
        log.info("weekly integrity finished", extra={"report": report.summary()})
        return 0
    log.error("integrity alert", extra={"report": report.summary()})
    return 1


def protocol(ctx: Context, args: argparse.Namespace) -> int:
    """``protocol freeze`` freezes the configured Protocol for ``--strategy`` (``pullback_v1``
    unless named) with the data snapshot read now, at this commit, and prints its Window
    boundaries; ``protocol windows`` prints the boundaries the configuration gives, writing
    nothing, for the Operator to check before freezing. ``pullback_v1``'s Terms and Limits
    are ``[protocol]`` and ``[risk]``; ``lowvol_v1``'s are ``[lowvol.protocol]`` and
    ``[lowvol.risk]``.

    The boundaries go to stderr; the summary is the last JSON line on stdout.
    """
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    strategy: AnyStrategy
    limit_set: Config.LimitSet
    if args.strategy == LowvolV1.name:
        strategy, limit_set = LowvolV1(), ctx.config.lowvol.risk
        terms = ctx.config.lowvol.protocol.terms()
    else:
        strategy, limit_set = PullbackV1(), ctx.config.risk
        terms = ctx.config.protocol.terms()
    if args.action == "windows":
        windows, holdout = window_boundaries(terms.layout, calendar)
        sys.stderr.write(windows_text(windows, holdout))
        return 0
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = freeze_protocol(
            conn,
            terms,
            strategy=strategy,
            rule_version=ctx.config.universe.version,
            limits=limit_set.limits(),
            halts=limit_set.halts(),
            schedule=load_cost_schedule(Path(ctx.config.costs.schedule)),
            slippage=ctx.config.costs.slippage(),
            calendar=calendar,
            commit=revision.running_commit(),
            **_tax_inputs(ctx, terms),
        )
    sys.stderr.write(result.text())
    log.info("protocol freeze finished", extra=result.summary())
    return 0


def _tax_inputs(ctx: Context, terms: Terms) -> dict[str, Any]:
    """What a Protocol keeping tax freezes beyond its Terms: ``[tax]`` and ``[benchmark]``."""
    if not terms.tax:
        return {}
    return {
        "tax_schedule": load_tax_schedule(Path(ctx.config.tax.schedule)),
        "dividend_rate": Decimal(str(ctx.config.tax.dividend_rate)),
        "benchmark_funds": load_benchmark_funds(Path(ctx.config.benchmark.funds)),
        "factor_fund": ctx.config.benchmark.factor_fund,
    }


def run(ctx: Context, args: argparse.Namespace) -> int:
    """One Run of ``pullback_v1``, written to the Ledger with a Verdict on each Candidate. In
    backtest mode it is a backtest of the day ``--date`` under the Protocol ``--protocol``
    names. In paper mode it is the paper dry-run's day on the wall clock (today in IST, or
    ``--date``) over what ``collect-daily`` loaded, under the configured Universe Rule and
    Limits: the Orders the dry run placed the day before fill against the day's end-of-day
    bar first, and each Fill is priced on its own day's rates unless ``--costs-as-of`` names
    a date."""
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    if args.mode == "backtest" and args.day is None:
        raise ValueError("run --mode backtest needs --date")
    clock = WallClock(day=args.day)
    (day,) = clock.days()
    with _scope(day, day), app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        if args.mode == "paper":
            # The same starting Equity and band a Protocol freezes from the configuration.
            terms = ctx.config.protocol.terms()
            costs = CostModel(
                load_cost_schedule(Path(ctx.config.costs.schedule)),
                ctx.config.costs.slippage(),
                rates_as_of=args.costs_as_of,
            )
            result = paper_service(
                conn,
                PullbackV1(),
                clock=clock,
                calendar=calendar,
                rule_version=ctx.config.universe.version,
                limits=ctx.config.risk.limits(),
                tick_sizes=tick_sizes,
                costs=costs,
                starting_equity=terms.starting_equity,
                band=terms.band,
            )
        else:
            (result,) = backtest_service(
                conn,
                PullbackV1(),
                protocol=args.protocol,
                start=day,
                end=day,
                calendar=calendar,
                tick_sizes=tick_sizes,
            )
    log.info("run finished", extra=result.summary())
    return 0


def backtest(ctx: Context, args: argparse.Namespace) -> int:
    """A backtest of ``pullback_v1`` at fixed parameters from ``--from`` to ``--to``: one Run a
    trading day under the Protocol ``--protocol`` names (its Universe Rule, Limits and
    costs), the Paper Broker filling each day's Orders against the next day's bar.
    Without ``--from``, the whole Protocol: Walk-Forward over its Windows, resumed where
    an interrupted run stopped, its report's JSON written to ``--out`` if given.
    ``backtest report`` reports the Protocol (or one cell)."""
    if args.backtest_command == "report":
        return backtest_report(ctx, args)
    if args.backtest_command == "holdout":
        return holdout(ctx, args)
    if args.backtest_command == "fresh":
        return fresh_period(ctx, args)
    if args.start is None:
        return walk_forward(ctx, args)
    end = args.end or args.start
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    strategy = PullbackV1(k=Decimal(args.k), r=Decimal(args.r), n=args.n)
    with _scope(args.start, end), app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        results = backtest_service(
            conn,
            strategy,
            protocol=args.protocol,
            start=args.start,
            end=end,
            calendar=calendar,
            tick_sizes=tick_sizes,
        )
    log.info(
        "backtest finished",
        extra={
            "backtest_id": str(results[0].backtest_id),
            "protocol": args.protocol,
            "parameters": dict(strategy.parameters()),
            "runs": len(results),
            "passes": sum(r.passes for r in results),
            "fills": sum(r.fills for r in results),
            "open_positions": results[-1].open_positions,
            "equity": str(results[-1].equity),
        },
    )
    return 0


def walk_forward(ctx: Context, args: argparse.Namespace) -> int:
    """Walk-Forward over the Protocol ``--protocol`` names, for the Strategy it validates."""
    if args.end is not None:
        raise ValueError("--to needs --from; without either, backtest runs the whole Protocol")
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = walk_forward_service(
            conn,
            _strategies(conn, args.protocol),
            protocol=args.protocol,
            calendar=calendar,
            tick_sizes=tick_sizes,
        )
    if args.out is not None:
        args.out.write_text(result.report.text())
    log.info("walk-forward finished", extra=result.summary())
    return 0


def holdout(ctx: Context, args: argparse.Namespace) -> int:
    """The Holdout of the Protocol ``--protocol`` names, once its Walk-Forward has finished:
    run once per version, a second run refused and recorded (ADR 0007)."""
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = run_holdout(
            conn,
            _strategies(conn, args.protocol),
            protocol=args.protocol,
            calendar=calendar,
            tick_sizes=tick_sizes,
        )
    log.info("holdout finished", extra=result.summary())
    return 0


def fresh_period(ctx: Context, args: argparse.Namespace) -> int:
    """The Fresh Period of the version the Protocol ``--protocol`` names, whose Holdout run
    is Not Independent: its Holdout's parameters in a fresh account over the trading days
    after the Holdout and the freeze, to the last day with data (ADR 0007)."""
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = run_fresh_period(
            conn,
            _strategies(conn, args.protocol),
            protocol=args.protocol,
            calendar=calendar,
            tick_sizes=tick_sizes,
        )
    log.info("fresh period finished", extra=result.summary())
    return 0


def _pullback(parameters: Mapping[str, Decimal]) -> PullbackV1:
    """``pullback_v1`` at a cell's parameters."""
    return PullbackV1(k=parameters["k"], r=parameters["r"], n=int(parameters["n"]))


def _lowvol(parameters: Mapping[str, Decimal]) -> LowvolV1:
    """``lowvol_v1``, which has no parameters."""
    if parameters:
        raise ValueError(f"lowvol_v1 has no parameters, not {sorted(parameters)}")
    return LowvolV1()


# Each Strategy a Protocol can validate, by name, made at a cell's parameters.
STRATEGIES: dict[str, StrategyFactory] = {
    PullbackV1.name: _pullback,
    LowvolV1.name: _lowvol,
}


def _strategies(conn: psycopg.Connection, protocol: str | None) -> StrategyFactory:
    """The Strategy the frozen Protocol ``protocol`` validates, at a cell's parameters;
    ``pullback_v1``'s when no Protocol of that name is frozen, for the service to refuse."""
    frozen = queries.protocol(conn, protocol) if protocol else None
    name = PullbackV1.name if frozen is None else frozen.protocol.name
    if name not in STRATEGIES:
        raise ValueError(f"Protocol {protocol} validates {name}, which this code does not know")
    return STRATEGIES[name]


def backtest_report(ctx: Context, args: argparse.Namespace) -> int:
    """The single-run report: the cell backtested from ``--from`` to ``--to`` under the
    Protocol ``--protocol`` names, and again with its Slippage impact at zero and at twice
    its value, scored and written as JSON to ``--out``. Without ``--from``, the full report
    on the Protocol (``protocol_report``)."""
    if args.start is None:
        return protocol_report(ctx, args)
    if args.markdown is not None:
        raise ValueError("--markdown is the full report's; leave out --from to write it")
    end = args.end or args.start
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    strategy = PullbackV1(k=Decimal(args.k), r=Decimal(args.r), n=args.n)
    notes = load_contract_notes(Path(ctx.config.costs.contract_notes))
    with _scope(args.start, end), app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = report_backtest(
            conn,
            strategy,
            protocol=args.protocol,
            start=args.start,
            end=end,
            calendar=calendar,
            tick_sizes=tick_sizes,
            contract_notes=len(notes),
        )
    args.out.write_text(result.report.text())
    log.info("backtest report finished", extra={**result.summary(), "out": str(args.out)})
    return 0


def protocol_report(ctx: Context, args: argparse.Namespace) -> int:
    """The full report on the Protocol ``--protocol`` names, once its Walk-Forward and Holdout
    have run: the c sensitivity written if missing, the Backtest Gate evaluated and recorded,
    the report's JSON written to ``--out`` and its Markdown to ``--markdown`` if given."""
    if args.end is not None:
        raise ValueError("--to needs --from; without either, the report is the whole Protocol's")
    calendar = load_trading_calendar(Path(ctx.config.integrity.trading_calendar))
    tick_sizes = load_tick_sizes(Path(ctx.config.risk.tick_sizes))
    notes = load_contract_notes(Path(ctx.config.costs.contract_notes))
    with app_connection(ctx.secrets.require(DATABASE_URL)) as conn:
        result = report_protocol(
            conn,
            _strategies(conn, args.protocol),
            protocol=args.protocol,
            calendar=calendar,
            tick_sizes=tick_sizes,
            contract_notes=len(notes),
        )
    args.out.write_text(result.report.text())
    if args.markdown is not None:
        args.markdown.write_text(result.report.markdown())
    log.info("backtest report finished", extra={**result.summary(), "out": str(args.out)})
    return 0


Command = Callable[[Context, argparse.Namespace], int]

# The frozen Protocol a backtest runs under.
PROTOCOL: tuple[tuple[str, ...], dict[str, Any]] = (
    ("--protocol",),
    {
        "metavar": "HASH",
        "default": None,
        "help": "the frozen Protocol to run under: its hash, or 12 or more characters of it",
    },
)

COMMANDS: dict[str, Command] = {
    "migrate": migrate,
    "login": login,
    "ingest": ingest,
    "universe": universe,
    "declare-rule-validity": declare_rule_validity,
    "backfill-universe-builds": backfill_universe_builds,
    "parity": parity,
    "compare-adjusted": compare_adjusted,
    "sectors": sectors,
    "rebuild-instruments": rebuild_instruments,
    "instruments": instruments,
    "verify-universe": verify_universe,
    "check-universe": check_universe,
    "collect-daily": collect_daily,
    "integrity": integrity,
    "integrity-weekly": integrity_weekly,
    "protocol": protocol,
    "run": run,
    "backtest": backtest,
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
        "demergers",
        help="the demerger table, read whole and stored as the table in force; a Protocol"
        " crediting demergers freezes its hash",
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
    benchmark = sources.add_parser(
        "benchmark",
        parents=[fixtures],
        help="AMFI's scheme master lines and NAV history of the Benchmark and the factor fund;"
        " fails unless AMFI names the Benchmark's scheme Direct and Growth",
    )
    benchmark.add_argument(
        "--to",
        dest="end",
        metavar="DATE",
        type=date.fromisoformat,
        help="the last NAV date to load; defaults to today in India",
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

    validity_parser = subparsers.add_parser(
        "declare-rule-validity",
        help="declare a Universe Rule version the rule in force from a date (Rule Validity);"
        " append-only, a later declaration supersedes from its own date",
    )
    validity_parser.add_argument(
        "--in-force-from", metavar="DATE", type=date.fromisoformat, required=True
    )
    validity_parser.add_argument(
        "--rule-version",
        metavar="VERSION",
        help="a recorded version or a unique prefix of one; defaults to the configured rule's",
    )
    subparsers.add_parser(
        "backfill-universe-builds",
        help="record a build for every stored Universe month without one, from the ingest run"
        " history, or marked cutoff-inferred where the history does not cover its window",
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
    check_parser = subparsers.add_parser(
        "check-universe",
        help="the Conformance Check: each stored Universe month is what the configured rule"
        " selects; exit 1 unless every month conforms; writes nothing",
    )
    check_parser.add_argument(
        "--month", metavar="YYYY-MM", type=parse_month, required=True, help="the first month"
    )
    check_parser.add_argument(
        "--to",
        metavar="YYYY-MM",
        type=parse_month,
        help="the last month; defaults to --month",
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

    parity_parser = subparsers.add_parser(
        "parity",
        parents=[date_range],
        help="Kite Parity: compare daily candles with NSE end-of-day files; exit 1 unless"
        " unexplained differences are under the threshold",
    )
    parity_parser.add_argument(
        "--symbol",
        dest="symbols",
        metavar="SYMBOL",
        action="append",
        help="an NSE symbol to compare; repeatable; defaults to every symbol with candles",
    )
    compare_parser = subparsers.add_parser(
        "compare-adjusted",
        help="Factor Agreement: judge each split and bonus Ex-Date of every ever-member,"
        " or of the named Symbols, by Kite's Adjustment; exit 1 if any disagrees",
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
        help="an NSE symbol to judge in place of the ever-members, its bars listed against"
        " Kite's to the paisa; repeatable",
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
    subparsers.add_parser(
        "integrity",
        parents=[date_range],
        help="check NSE's end-of-day files against the Trading Calendar, never a broker;"
        " exit 1 unless every check passes",
    )
    weekly_parser = subparsers.add_parser(
        "integrity-weekly",
        help="the Integrity Check over every day after a Protocol's freeze, with its Kite Parity"
        " gaps (never failures); exit 1 and log an integrity alert if it fails",
    )
    after = weekly_parser.add_mutually_exclusive_group(required=True)
    after.add_argument(
        "--protocol",
        metavar="HASH",
        help="the Protocol (12 or more characters of its hash) whose freeze day the check"
        " starts after",
    )
    after.add_argument(
        "--after",
        metavar="DATE",
        type=date.fromisoformat,
        help="the day the check starts after, in place of a Protocol's freeze day",
    )
    weekly_parser.add_argument(
        "--to",
        dest="end",
        metavar="DATE",
        type=date.fromisoformat,
        help="the last day checked, inclusive; defaults to today in IST",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="one Run of the Strategy as of a day over stored data, written to the Ledger",
    )
    run_parser.add_argument("--mode", choices=["backtest", "paper"], required=True)
    run_parser.add_argument(
        "--date",
        dest="day",
        metavar="DATE",
        type=date.fromisoformat,
        default=None,
        help="the Run's day: required in backtest mode; in paper mode today in IST by default",
    )
    run_parser.add_argument(*PROTOCOL[0], **PROTOCOL[1])
    run_parser.add_argument(
        "--costs-as-of",
        metavar="DATE",
        type=date.fromisoformat,
        default=None,
        help="paper mode: price every Fill on the cost schedule in force on DATE (default each"
        " Fill's own day); a backtest's date is its Protocol's",
    )
    # One cell: the Strategy at fixed parameters, under a frozen Protocol.
    cell = argparse.ArgumentParser(add_help=False)
    defaults = PullbackV1()
    cell.add_argument(
        "--k", default=str(defaults.k), help="stop distance in ATRs (default %(default)s)"
    )
    cell.add_argument(
        "--r", default=str(defaults.r), help="target in multiples of the risk (default %(default)s)"
    )
    cell.add_argument(
        "--n", type=int, default=defaults.n, help="time stop in trading days (default %(default)s)"
    )
    cell.add_argument(*PROTOCOL[0], **PROTOCOL[1])
    backtest_parser = subparsers.add_parser(
        "backtest",
        parents=[cell],
        help="backtest the Strategy at fixed parameters over stored data, one Run a trading"
        " day, written to the Ledger with its Orders, Fills and Positions",
    )
    # Not required here, so ``backtest report --from`` parses; ``backtest`` refuses its absence.
    backtest_parser.add_argument("--from", dest="start", metavar="DATE", type=date.fromisoformat)
    backtest_parser.add_argument(
        "--to", dest="end", metavar="DATE", type=date.fromisoformat, help="inclusive"
    )
    backtest_parser.add_argument(
        "--out",
        metavar="FILE",
        type=Path,
        help="without --from (the whole Protocol): where the Walk-Forward report's JSON is written",
    )
    reports = backtest_parser.add_subparsers(dest="backtest_command")
    report_parser = reports.add_parser(
        "report",
        parents=[cell],
        help="the full report: evaluate and record the Backtest Gate on the Protocol's"
        " Stitched Curve and Holdout, with the c sensitivity, and write its JSON and Markdown;"
        " with --from, backtest one cell and again with c at zero and at twice its value",
    )
    report_parser.add_argument(
        "--from",
        dest="start",
        metavar="DATE",
        type=date.fromisoformat,
        help="report one cell from DATE instead of the whole Protocol",
    )
    report_parser.add_argument(
        "--to", dest="end", metavar="DATE", type=date.fromisoformat, help="inclusive"
    )
    report_parser.add_argument(
        "--out", metavar="FILE", type=Path, required=True, help="where the JSON is written"
    )
    report_parser.add_argument(
        "--markdown",
        metavar="FILE",
        type=Path,
        help="the full report: where its Markdown is written",
    )
    holdout_parser = reports.add_parser(
        "holdout",
        help="run the Protocol's Holdout once, after its Walk-Forward, under the last Window's"
        " parameters; a second run by the same version is refused and recorded",
    )
    holdout_parser.add_argument(*PROTOCOL[0], **PROTOCOL[1])
    fresh_parser = reports.add_parser(
        "fresh",
        help="run a Not Independent version's Fresh Period under its Holdout's parameters,"
        " from a fresh account to the last day with data, for the Backtest Gate to score",
    )
    fresh_parser.add_argument(*PROTOCOL[0], **PROTOCOL[1])
    protocol_parser = subparsers.add_parser(
        "protocol", help="the Protocol a Strategy version is validated under (ADR 0007)"
    )
    protocol_actions = protocol_parser.add_subparsers(dest="action", required=True)
    named = argparse.ArgumentParser(add_help=False)
    named.add_argument(
        "--strategy",
        choices=sorted(STRATEGIES),
        default=PullbackV1.name,
        help="the Strategy whose configured Protocol it is (default %(default)s)",
    )
    protocol_actions.add_parser(
        "freeze",
        parents=[named],
        help="freeze the configured Protocol with the data snapshot and the cost schedule as of"
        " now, at this commit, and print its Window boundaries as trading days",
    )
    protocol_actions.add_parser(
        "windows",
        parents=[named],
        help="print the configured Window boundaries as trading days, writing nothing",
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
        UnreadableMigrations,
        NotAppRoleMember,
        ValueError,  # ConfigError is a ValueError
    ) as error:
        log.error(str(error))
        return 1
    except Exception:
        log.exception("command failed")
        return 1
