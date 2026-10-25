"""The Relative Drawdown Halt's threshold when the index history is or is not stored, through
``tradeline ingest index-history`` and ``tradeline protocol freeze --strategy lowvol_v1``.

The market is ``synthetic_eod.lowvol_market``, its Benchmark NAVs loaded, with no index
history at first. The fallback is NSE's June 2018 white paper, Exhibit 4: the worst
calendar year against the NIFTY 100 is 2007, 29.4 percent against 57.5 percent, a
shortfall of 1 - 1.294 / 1.575 = 0.178413 (to six places), rounded up to 20 percent.
"""

import contextlib
import io
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from synthetic_eod import (
    INDEX_HISTORY_FIXTURES,
    LOWVOL_LAYOUT,
    LOWVOL_RANGE,
    REPO_ROOT,
    compute_universe_months,
    config_text,
    load_benchmark,
    load_demerger_table,
    load_market,
    lowvol_benchmark_navs,
    lowvol_market,
    repo_calendar,
    write_benchmark_fixtures,
    write_fixtures,
)
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection

D = Decimal


@pytest.fixture(scope="module")
def market(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """The database with the market and the Benchmark loaded, and its configuration."""
    calendar = repo_calendar()
    market = lowvol_market()
    directory = tmp_path_factory.mktemp("threshold")
    fixtures = write_fixtures(market, directory / "fixtures", calendar)
    navs = write_benchmark_fixtures(
        directory / "amfi", lowvol_benchmark_navs(calendar), end=LOWVOL_RANGE[1]
    )
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        compute_universe_months(conn, market, *LOWVOL_RANGE)
        load_benchmark(conn, navs, end=LOWVOL_RANGE[1])
        load_demerger_table(conn)
    layout = LOWVOL_LAYOUT
    config = directory / "tradeline.toml"
    config.write_text(
        config_text(market) + "[lowvol.protocol]\n"
        f'holdout_start = "{layout.holdout_start:%Y-%m}"\n'
        f"holdout_months = {layout.holdout_months}\n"
        f"windows = {layout.windows}\n"
        f"out_of_sample_months = {layout.out_of_sample_months}\n"
        f"in_sample_months = {layout.in_sample_months}\n"
        f'[benchmark]\nfunds = "{REPO_ROOT / "benchmark_funds.csv"}"\n'
    )
    return database_url, config


def _cli(database_url: str, config: Path, *argv: str) -> tuple[int, str]:
    """The command's exit code and what it logged."""
    logged = io.StringIO()
    with (
        pytest.MonkeyPatch.context() as patch,
        contextlib.redirect_stdout(logged),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        code = main(list(argv))
    return code, logged.getvalue()


def _protocols(database_url: str) -> list[queries.FrozenProtocol]:
    with app_connection(database_url) as conn:
        return queries.protocols(conn)


def _index_values(database_url: str) -> int:
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT count(*) FROM index_values").fetchone()
    assert row is not None
    return int(row[0])


def test_without_index_history_the_freeze_is_refused_unless_the_fallback_is_asked_for(
    market: tuple[str, Path],
) -> None:
    database_url, config = market

    code, logged = _cli(database_url, config, "protocol", "freeze", "--strategy", "lowvol_v1")

    assert code == 1
    assert "ingest index-history" in logged
    assert "--relative-drawdown-fallback" in logged
    assert _protocols(database_url) == []


def test_the_fallback_threshold_is_the_white_papers_worst_year_recorded_as_a_fallback(
    market: tuple[str, Path],
) -> None:
    database_url, config = market

    code, _ = _cli(
        database_url,
        config,
        "protocol",
        "freeze",
        "--strategy",
        "lowvol_v1",
        "--relative-drawdown-fallback",
    )

    assert code == 0
    (frozen,) = _protocols(database_url)
    relative = frozen.protocol.halts.relative_drawdown
    assert relative is not None
    assert relative.fallback
    assert (relative.threshold, relative.worst) == (D("0.20"), D("0.178413"))
    assert "White_paper_Jun2018" in relative.source
    assert "2007" in relative.source
    assert "not the same measure" in relative.note
    assert relative.history == ()
    assert frozen.protocol.content()["halts"]["relative_drawdown"]["fallback"] is True


def test_once_the_index_history_is_stored_the_fallback_is_refused(
    market: tuple[str, Path],
) -> None:
    database_url, config = market
    before = _protocols(database_url)

    loaded, _ = _cli(
        database_url,
        config,
        "ingest",
        "index-history",
        "--play-fixtures",
        str(INDEX_HISTORY_FIXTURES),
    )
    stored = _index_values(database_url)
    again, _ = _cli(
        database_url,
        config,
        "ingest",
        "index-history",
        "--play-fixtures",
        str(INDEX_HISTORY_FIXTURES),
    )
    code, logged = _cli(
        database_url,
        config,
        "protocol",
        "freeze",
        "--strategy",
        "lowvol_v1",
        "--relative-drawdown-fallback",
    )

    assert (loaded, again) == (0, 0)
    # Two indices, 3,595 closes each from 2005-04-01 to 2019-09-30; the second run adds none.
    assert stored == _index_values(database_url) == 2 * 3595
    assert code == 1
    assert "cannot be served" in logged
    assert _protocols(database_url) == before
