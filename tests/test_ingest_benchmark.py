"""``ingest benchmark`` by playback of recorded AMFI responses, asserted through the reads.

The fixtures (``fixtures/ingest_benchmark/README.md``) are AMFI's scheme master
of 2026-10-23, trimmed to the Motilal Oswal Nifty 500 Index Fund's two plans and
the ICICI Prudential Nifty 100 Low Vol 30 ETF, and the NAV history of 147625 and
141596 from 2019-09-11 to 2026-10-23 in its two four-year windows, each trimmed
to its first and last 25 NAVs. Values below are read off the recorded bodies.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from playback import Playback, lines
from synthetic_eod import REPO_ROOT
from tradeline import queries
from tradeline.benchmark import (
    BenchmarkFunds,
    Nav,
    NotDirectGrowth,
    benchmark_nav,
    benchmark_scheme,
    load_benchmark_funds,
    nav_as_of,
    navs,
)
from tradeline.db import app_connection
from tradeline.ingest.benchmark import BenchmarkIngestResult, ingest_benchmark

FIXTURES = Path(__file__).parent / "fixtures" / "ingest_benchmark"
FUNDS = load_benchmark_funds(REPO_ROOT / "benchmark_funds.csv")
BENCHMARK, FACTOR_FUND = 147625, 141596
END = date(2026, 10, 23)
D = Decimal


@dataclass(frozen=True)
class TwoRuns:
    first: BenchmarkIngestResult
    second: BenchmarkIngestResult
    playback: Playback
    sleeps: list[float]


class FakeClock:
    """Time that moves only when slept."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def ingest(
    conn: psycopg.Connection, playback: Playback, *, funds: BenchmarkFunds = FUNDS
) -> BenchmarkIngestResult:
    clock = FakeClock()
    return ingest_benchmark(
        conn,
        playback,
        funds=funds,
        factor_fund=FACTOR_FUND,
        end=END,
        clock=clock.clock,
        sleep=clock.sleep,
    )


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> TwoRuns:
    with app_connection(database_url) as conn:
        playback = Playback(FIXTURES)
        clock = FakeClock()
        first = ingest_benchmark(
            conn,
            playback,
            funds=FUNDS,
            factor_fund=FACTOR_FUND,
            end=END,
            clock=clock.clock,
            sleep=clock.sleep,
        )
        second = ingest(conn, Playback(FIXTURES))
    return TwoRuns(first, second, playback, clock.sleeps)


@pytest.fixture
def conn(database_url: str, two_runs: TwoRuns) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_the_benchmark_is_the_direct_growth_plan_named_in_amfis_master(
    conn: psycopg.Connection,
) -> None:
    found = benchmark_scheme(conn, FUNDS, END)

    assert (found.scheme_code, found.full_name, found.isin_growth) == (
        BENCHMARK,
        "Motilal Oswal Nifty 500 Index Fund Direct Plan Growth",
        "INF247L01957",
    )


def test_the_nav_history_is_read_from_playback_for_the_benchmark_and_the_factor_fund(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    assert benchmark_nav(conn, FUNDS, date(2019, 9, 11)) == Nav(
        BENCHMARK, date(2019, 9, 11), D("10.0936")
    )
    assert benchmark_nav(conn, FUNDS, END) == Nav(BENCHMARK, END, D("25.0677"))
    assert nav_as_of(conn, FACTOR_FUND, date(2019, 9, 11)) == Nav(
        FACTOR_FUND, date(2019, 9, 11), D("87.55")
    )
    assert nav_as_of(conn, FACTOR_FUND, END) == Nav(FACTOR_FUND, END, D("20.4749"))
    assert two_runs.first.rows_written == 4 * 50 + 2


def test_each_scheme_is_asked_for_in_four_year_windows_about_four_seconds_apart(
    two_runs: TwoRuns,
) -> None:
    history = [c.url for c in two_runs.playback.calls if "nav-history" in c.url]

    assert history == [
        f"https://www.amfiindia.com/api/nav-history?query_type=historical_period"
        f"&from_date={start}&to_date={end}&sd_id={code}"
        for code in (BENCHMARK, FACTOR_FUND)
        for start, end in (("2019-09-11", "2023-09-10"), ("2023-09-11", "2026-10-23"))
    ]
    # The master, then four history calls: each after a wait of 4 s.
    assert two_runs.sleeps == [4.0] * 4


def test_a_second_run_writes_nothing(two_runs: TwoRuns) -> None:
    assert (two_runs.second.rows_seen, two_runs.second.rows_written) == (
        two_runs.first.rows_seen,
        0,
    )


def test_a_nav_read_as_of_a_day_never_returns_a_later_one(conn: psycopg.Connection) -> None:
    """AMFI published no NAV on 2019-10-02 (Gandhi Jayanti) or before 2019-09-11."""
    holiday = nav_as_of(conn, BENCHMARK, date(2019, 10, 2))
    assert holiday is not None and holiday.nav_date == date(2019, 10, 1)
    assert nav_as_of(conn, BENCHMARK, date(2019, 9, 10)) is None
    for day in (date(2019, 10, 5), date(2023, 9, 30), date(2026, 10, 25)):
        found = nav_as_of(conn, BENCHMARK, day)
        assert found is not None and found.nav_date <= day
        later = navs(conn, BENCHMARK, start=found.nav_date, end=day)
        assert [n.nav_date for n in later] == [found.nav_date]


def without_growth(text: list[str]) -> list[str]:
    return [line.replace(";Direct Plan;Growth;", ";Direct Plan;;") for line in text]


def without_direct(text: list[str]) -> list[str]:
    return [line.replace(";Direct Plan;Growth;", ";Regular Plan;Growth;") for line in text]


@pytest.mark.parametrize("edit", [without_growth, without_direct])
def test_a_master_naming_the_benchmark_without_direct_or_growth_fails_and_stores_nothing(
    database_url: str, two_runs: TwoRuns, edit: object
) -> None:
    with app_connection(database_url) as conn:
        before = navs(conn, BENCHMARK, start=date(2019, 1, 1), end=END)
        playback = Playback(FIXTURES, edit=lines(edit))  # type: ignore[arg-type]

        with pytest.raises(NotDirectGrowth, match="147625"):
            ingest(conn, playback)

        assert not [c for c in playback.calls if "nav-history" in c.url]
        assert navs(conn, BENCHMARK, start=date(2019, 1, 1), end=END) == before
        latest = queries.ingest_runs(conn, source="amfi")[0]
        assert latest.error is not None and "NotDirectGrowth" in latest.error


def test_a_fund_change_to_a_scheme_not_named_direct_growth_fails_the_build(
    database_url: str, two_runs: TwoRuns, tmp_path: Path
) -> None:
    """The factor fund, an ETF, has neither plan nor option in AMFI's master."""
    path = tmp_path / "funds.csv"
    path.write_text(
        "in_force_from,scheme_code,event,citation\n"
        "2019-09-11,147625,chosen,X\n2024-01-01,141596,merger,X\n"
    )
    with app_connection(database_url) as conn, pytest.raises(NotDirectGrowth, match="141596"):
        ingest(conn, Playback(FIXTURES), funds=load_benchmark_funds(path))
