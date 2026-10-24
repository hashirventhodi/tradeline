"""``protocol freeze`` and the engine's refusals under it, over the synthetic market.

The market is ``synthetic_eod.base_market`` (2022-01 to 2024-06, six stocks,
a five-member Universe ranking Symbols, its rule declared in force from
2022-01, so every month is Recorded). The configured Protocol puts the Holdout
at 2024-01 for six months, with two Out-of-Sample Windows of three months
anchored back from it, each after six In-Sample months. Read off NSE's
calendar (2023-09-28 and 2023-10-02 closed, every other weekday here open):

- Window 1: In-Sample 2023-01-02 to 2023-06-30, Out-of-Sample 2023-07-03 to
  2023-09-29.
- Window 2: In-Sample 2023-04-03 to 2023-09-29, Out-of-Sample 2023-10-03 to
  2023-12-29.
- Holdout: 2024-01-01 to 2024-06-28.

So the Protocol covers the Universe months 2023-01 to 2024-06, eighteen, whose
members are ALPHA, BETA, DELTA, EPSILON and GAMMA (OMEGA trades too little).
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from checkout import TEST_COMMIT, dirty_checkout
from synthetic_eod import (
    COSTS_AS_OF,
    GRID,
    REPO_ROOT,
    base_market,
    compute_universe_months,
    freeze_protocol,
    load_market,
    protocol_terms,
    repo_calendar,
    write_fixtures,
)
from tradeline import queries, revision
from tradeline.cli import main
from tradeline.costs import CostModel, Slippage, load_cost_schedule
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.engine import backtest
from tradeline.metrics import HaltLimits
from tradeline.protocol import TIE_BREAK, Gate, Span, Window, content_hash
from tradeline.revision import code_revision
from tradeline.risk import Limits
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes

D = Decimal
MARKET = base_market()
FIRST, LAST = date(2023, 1, 2), date(2024, 6, 28)
WINDOWS = (
    Window(Span(date(2023, 1, 2), date(2023, 6, 30)), Span(date(2023, 7, 3), date(2023, 9, 29))),
    Window(Span(date(2023, 4, 3), date(2023, 9, 29)), Span(date(2023, 10, 3), date(2023, 12, 29))),
)
HOLDOUT = Span(date(2024, 1, 1), date(2024, 6, 28))
MEMBERS = ["ALPHA", "BETA", "DELTA", "EPSILON", "GAMMA"]
TICK_SIZES = load_tick_sizes(REPO_ROOT / "tick_sizes.csv")
PROTOCOL_TOML = (
    '[protocol]\nholdout_start = "2024-01"\nholdout_months = 6\nwindows = 2\n'
    "out_of_sample_months = 3\nin_sample_months = 6\n"
)


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_fixtures(MARKET, tmp_path_factory.mktemp("synthetic"), repo_calendar())


@pytest.fixture(scope="module")
def loaded(database_url: str, fixtures: Path) -> str:
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        compute_universe_months(conn, MARKET, FIRST, LAST)
    return database_url


@pytest.fixture
def conn(loaded: str) -> Iterator[psycopg.Connection]:
    with app_connection(loaded) as conn:
        yield conn


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loaded: str) -> Path:
    rule = MARKET.rule
    path = tmp_path / "tradeline.toml"
    path.write_text(
        f"[universe]\ntop_n = {rule.top_n}\nlookback_trading_days = {rule.lookback_trading_days}\n"
        f"min_listing_trading_days = {rule.min_listing_trading_days}\n"
        f'[integrity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[parity]\ntrading_calendar = "{REPO_ROOT / "trading_calendar.csv"}"\n'
        f'[risk]\ntick_sizes = "{REPO_ROOT / "tick_sizes.csv"}"\n'
        "daily_loss_halt = 0.02\ndrawdown_halt = 0.08\n"
        f'[costs]\nschedule = "{REPO_ROOT / "cost_rates.csv"}"\n' + PROTOCOL_TOML
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(path))
    monkeypatch.setenv("DATABASE_URL", loaded)
    return path


def freeze_cli() -> list[str]:
    return ["protocol", "freeze"]


def test_protocol_freeze_writes_the_protocol_once_and_prints_its_window_boundaries(
    config: Path, conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(freeze_cli()) == 0

    printed = capsys.readouterr().err
    assert "1       2023-01-02 .. 2023-06-30  2023-07-03 .. 2023-09-29" in printed
    assert "2       2023-04-03 .. 2023-09-29  2023-10-03 .. 2023-12-29" in printed
    assert "Holdout 2024-01-01 .. 2024-06-28" in printed
    (frozen,) = queries.protocols(conn)
    assert frozen.protocol.windows == WINDOWS
    assert frozen.protocol.holdout == HOLDOUT
    # The configured Halts, so the days a report lists are the frozen ones.
    assert frozen.protocol.halts == HaltLimits(daily_loss=D("0.02"), drawdown=D("0.08"))
    # The cost schedule as of the freeze: the day it was frozen, in India.
    assert frozen.protocol.costs_as_of == frozen.frozen_at.astimezone(IST).date()

    assert main(freeze_cli()) == 0

    assert "already frozen, nothing written" in capsys.readouterr().err
    assert queries.protocols(conn) == [frozen]


def test_protocol_freeze_takes_no_cost_schedule_date_but_the_freeze_s_own(
    config: Path, conn: psycopg.Connection
) -> None:
    before = queries.protocols(conn)

    with pytest.raises(SystemExit):
        main(["protocol", "freeze", "--costs-as-of", COSTS_AS_OF.isoformat()])

    assert queries.protocols(conn) == before


def test_protocol_windows_prints_the_boundaries_and_freezes_nothing(
    config: Path, conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    before = queries.protocols(conn)

    assert main(["protocol", "windows"]) == 0

    assert "Holdout 2024-01-01 .. 2024-06-28" in capsys.readouterr().err
    assert queries.protocols(conn) == before


def test_the_same_content_in_another_key_order_or_decimal_form_has_the_same_hash() -> None:
    content = {"band": D("0.05"), "grid": {"k": [D("2"), D("2.5")]}, "starting_equity": D(500000)}
    reordered = {
        "starting_equity": D("5E+5"),
        "grid": {"k": [D("2.00"), D("2.50")]},
        "band": D("0.050"),
    }

    assert content_hash(reordered) == content_hash(content)
    assert content_hash({**content, "band": D("0.06")}) != content_hash(content)


def test_a_protocol_frozen_from_the_same_terms_in_another_decimal_form_is_the_same_one(
    conn: psycopg.Connection,
) -> None:
    first = freeze_protocol(conn, MARKET, FIRST, LAST)
    terms = protocol_terms(FIRST, LAST)
    written_otherwise = replace(
        terms,
        grid={axis: tuple(v.quantize(D("0.01")) for v in values) for axis, values in GRID.items()},
        band=D("0.0500"),
        gate=replace(terms.gate, sharpe_above=D("1.000")),
    )

    again = freeze_protocol(conn, MARKET, FIRST, LAST, terms=written_otherwise)

    assert again.hash == first.hash
    assert not again.written


def test_every_protocol_field_is_readable_through_the_query_layer(
    conn: psycopg.Connection,
) -> None:
    frozen = freeze_protocol(conn, MARKET, FIRST, LAST)

    found = queries.protocol(conn, frozen.hash[:12])

    assert found is not None
    assert found.hash == frozen.hash
    protocol = found.protocol
    assert (protocol.family, protocol.name) == ("pullback", "pullback_v1")
    assert protocol.code_revision == code_revision("tradeline.strategy.pullback")
    assert protocol.git_commit == TEST_COMMIT
    assert protocol.grid == {
        "k": (D("1.5"), D(2), D("2.5"), D(3)),
        "r": (D("1.5"), D(2), D(3)),
        "n": (D(5), D(10), D(15)),
    }
    assert "neighbours" in protocol.objective
    assert protocol.min_in_sample_trades == 30
    assert protocol.tie_break == TIE_BREAK
    assert protocol.windows == (
        Window(
            Span(date(2023, 1, 2), date(2024, 4, 30)), Span(date(2024, 5, 2), date(2024, 5, 31))
        ),
    )
    assert protocol.holdout == Span(date(2024, 6, 3), date(2024, 6, 28))
    assert protocol.costs_as_of == COSTS_AS_OF
    assert protocol.rates == load_cost_schedule(REPO_ROOT / "cost_rates.csv").rates(COSTS_AS_OF)
    assert protocol.slippage == Slippage(margin=D("0.0005"), impact=D("0.1"))
    assert protocol.band == D("0.05")
    assert protocol.limits == Limits()
    assert protocol.limits.participation_cap == D("0.01")
    assert protocol.halts == HaltLimits(daily_loss=D("0.03"), drawdown=D("0.10"))
    assert protocol.starting_equity == D(500000)
    assert protocol.risk_free_rate == D("0.065")
    assert protocol.gate == Gate(D("1.0"), D("0.15"), D("1.3"), 150)
    assert protocol.rule_version == MARKET.rule.version
    assert protocol.warm_up_trading_days == 250
    snapshot = protocol.snapshot
    # 250 trading days before 2023-01-02: 2022 had 248 (13 weekday holidays, one Muhurat
    # session), then 2021-12-31 and 2021-12-30.
    assert snapshot.start == date(2021, 12, 30)
    assert snapshot.end == LAST
    months = [m.month for m in snapshot.months]
    assert (months[0], months[-1], len(months)) == (date(2023, 1, 1), date(2024, 6, 1), 18)
    assert {m.mode for m in snapshot.months} == {"recorded"}
    for month in snapshot.months:
        (build,) = queries.universe_month_builds(
            conn, month=month.month, rule_version=MARKET.rule.version
        )
        assert (month.data_cutoff, month.eod_hash, month.links_hash) == (
            build.data_cutoff,
            build.eod_hash,
            build.links_hash,
        )
        assert month.eod_run_ids == build.eod_run_ids
    assert sorted(snapshot.adjusted_series) == MEMBERS
    assert snapshot.adjusted_series["ALPHA"] == queries.adjusted_series_hash(
        conn, "ALPHA", start=date(2021, 12, 30), end=LAST
    )


def test_a_backtest_under_a_protocol_records_it_with_the_strategy_version(
    conn: psycopg.Connection,
) -> None:
    frozen = freeze_protocol(conn, MARKET, FIRST, LAST)
    day = date(2023, 6, 15)

    (result,) = backtest(
        conn,
        PullbackV1(),
        protocol=frozen.hash,
        start=day,
        end=day,
        calendar=repo_calendar(),
        tick_sizes=TICK_SIZES,
    )

    (ran,) = queries.runs(conn, backtest_id=result.backtest_id)
    assert ran.strategy.protocol == frozen.hash
    assert ran.equity == D(500000)


def test_a_backtest_naming_no_protocol_is_refused(conn: psycopg.Connection) -> None:
    for name, reason in [(None, "names none"), ("0" * 64, "no Protocol 0{64} is frozen")]:
        with pytest.raises(ValueError, match=reason):
            backtest(
                conn,
                PullbackV1(),
                protocol=name,
                start=FIRST,
                end=FIRST,
                calendar=repo_calendar(),
                tick_sizes=TICK_SIZES,
            )


def test_the_backtest_command_without_a_protocol_is_refused_with_the_reason(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["backtest", "--from", "2023-06-15", "--to", "2023-06-15"]) == 1

    assert "runs under a frozen Protocol and names none" in capsys.readouterr().out


@pytest.fixture
def dirty_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The code run from a checkout with a tracked file changed since its commit."""
    checkout = dirty_checkout(tmp_path / "checkout")
    monkeypatch.setattr(revision, "running_commit", lambda: revision.git_commit(checkout))


@pytest.mark.usefixtures("dirty_tree")
def test_a_backtest_from_a_dirty_working_tree_is_refused(conn: psycopg.Connection) -> None:
    frozen = freeze_protocol(conn, MARKET, FIRST, LAST)

    with pytest.raises(ValueError, match="uncommitted changes"):
        backtest(
            conn,
            PullbackV1(),
            protocol=frozen.hash,
            start=FIRST,
            end=FIRST,
            calendar=repo_calendar(),
            tick_sizes=TICK_SIZES,
        )


@pytest.mark.usefixtures("dirty_tree")
def test_protocol_freeze_from_a_dirty_working_tree_is_refused(
    config: Path, conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    before = queries.protocols(conn)

    assert main(freeze_cli()) == 1

    assert "uncommitted changes" in capsys.readouterr().out
    assert queries.protocols(conn) == before


def test_a_backtest_under_a_protocol_frozen_at_another_commit_is_refused(
    conn: psycopg.Connection,
) -> None:
    elsewhere = freeze_protocol(conn, MARKET, FIRST, LAST, commit="0" * 40)

    with pytest.raises(ValueError, match=f"frozen at commit {'0' * 40} and this is commit"):
        backtest(
            conn,
            PullbackV1(),
            protocol=elsewhere.hash,
            start=FIRST,
            end=FIRST,
            calendar=repo_calendar(),
            tick_sizes=TICK_SIZES,
        )


@pytest.mark.parametrize(
    ("strategy", "start", "end", "refusal"),
    [
        (PullbackV1(n=30), FIRST, FIRST, "n = 30 is not on Protocol"),
        (PullbackV1(), date(2022, 12, 30), FIRST, "runs outside Protocol"),
        (PullbackV1(), LAST, date(2024, 7, 1), "runs outside Protocol"),
    ],
    ids=["off-the-grid", "before-the-first-window", "after-the-holdout"],
)
def test_a_backtest_off_the_protocols_grid_or_outside_its_range_is_refused(
    conn: psycopg.Connection, strategy: PullbackV1, start: date, end: date, refusal: str
) -> None:
    frozen = freeze_protocol(conn, MARKET, FIRST, LAST)

    with pytest.raises(ValueError, match=refusal):
        backtest(
            conn,
            strategy,
            protocol=frozen.hash,
            start=start,
            end=end,
            calendar=repo_calendar(),
            tick_sizes=TICK_SIZES,
        )


def test_an_end_of_day_row_changed_after_the_freeze_refuses_the_next_backtest_naming_it(
    fresh_database_url: str, fixtures: Path
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_market(conn, fixtures, MARKET)
        frozen = freeze_protocol(conn, MARKET, FIRST, LAST)
    # DELTA's close on 2023-07-12 is in 2023-08's Universe window and in its Adjusted Series.
    with psycopg.connect(fresh_database_url) as owner:
        owner.execute(
            "UPDATE eod_bhavcopy SET close = close + 1"
            " WHERE symbol = 'DELTA' AND trade_date = '2023-07-12'"
        )

    with app_connection(fresh_database_url) as conn:
        with pytest.raises(ValueError, match=r"Universe month 2023-08, Instrument DELTA;"):
            backtest(
                conn,
                PullbackV1(),
                protocol=frozen.hash,
                start=date(2023, 8, 1),
                end=date(2023, 8, 1),
                calendar=repo_calendar(),
                tick_sizes=TICK_SIZES,
            )
        assert queries.runs(conn, mode="backtest") == []


def test_a_frozen_protocol_cannot_be_changed_or_claim_a_hash_its_content_lacks(
    conn: psycopg.Connection,
) -> None:
    frozen = freeze_protocol(conn, MARKET, FIRST, LAST)

    for statement in ("UPDATE protocols SET content = '{}'", "DELETE FROM protocols"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO protocols (hash, content) VALUES (%s, '{}')", (frozen.hash[::-1],)
        )
    conn.rollback()
    assert queries.protocol(conn, frozen.hash) is not None


def test_a_cost_model_that_does_not_price_as_the_protocol_states_is_refused(
    conn: psycopg.Connection,
) -> None:
    frozen = freeze_protocol(conn, MARKET, FIRST, LAST)
    schedule = load_cost_schedule(REPO_ROOT / "cost_rates.csv")

    with pytest.raises(ValueError, match="does not price as Protocol"):
        backtest(
            conn,
            PullbackV1(),
            protocol=frozen.hash,
            start=FIRST,
            end=FIRST,
            calendar=repo_calendar(),
            tick_sizes=TICK_SIZES,
            costs=CostModel(schedule, Slippage(impact=D(0)), rates_as_of=COSTS_AS_OF),
        )
