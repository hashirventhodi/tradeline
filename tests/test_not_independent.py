"""A first version recorded Not Independent at freeze, and its Fresh Period (#99; ADR 0007
amendment of 2026-10-25), end to end: ``lowvol_v1``'s Protocol frozen from ``[lowvol]``,
its Walk-Forward, its Holdout, ``tradeline backtest fresh`` and ``tradeline backtest
report --protocol P``.

The market is ``synthetic_eod.lowvol_market`` run on to 2023-10-03. The Stitched Curve is
2022-08 (its Initial Buy decided 2022-07-29), the Holdout 2022-09 (its Initial Buy decided
2022-08-30; the Rebalance decided on its last day, 2022-09-30, trades after it and is not
counted, so it counts none). The version is frozen on 2022-09-30 (the owner's connection
moves the freeze time, as history this suite cannot replay), so its Fresh Period starts
2022-10-03, with its Initial Buy decided 2022-09-30, the last trading day of a September:
an Initial Buy, not a Rebalance. Its twelve months run to 2023-10-02, a holiday, so they
are due by 2023-09-29.

With data to 2023-09-29 the Fresh Period is twelve months long and holds one counted
Rebalance, decided 2023-03-31: the one decided on 2023-09-29 is on its last day. One more
day of data, 2023-10-03, counts the second.

The Benchmark's NAV falls 1 paisa a trading day from Rs 20, while the Strategy's flat
stocks only swing, so each curve beats it within its drawdown, and the Stitched Curve's
Rebalance minimum is set to 0 here: the outcome, once the Fresh Period may carry the Gate,
is decided by the Fresh Period's thresholds alone.
"""

import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg import connect

from synthetic_eod import (
    BENCHMARK_SCHEME,
    FACTOR_FUND,
    cli,
    cli_ok,
    compute_universe_months,
    daily_navs,
    load_benchmark,
    load_demerger_table,
    load_index_history,
    lowvol_config,
    lowvol_market,
    pass_holdout_checks,
    repo_calendar,
    write_amfi_fixtures,
    write_fixtures,
)
from tradeline import queries, revision
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.demergers import load_demergers
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.demergers import ingest_demergers
from tradeline.ingest.eod import ingest_eod
from tradeline.transport import PlaybackTransport
from tradeline.universe import declare_rule_validity

D = Decimal
CALENDAR = repo_calendar()
FROZEN_ON = date(2022, 9, 30)
FRESH_FIRST = date(2022, 10, 3)
ONE_REBALANCE = date(2023, 9, 29)
TWO_REBALANCES = date(2023, 10, 3)
NOT_INDEPENDENT = "the family was chosen after reading results covering the Holdout's years"


def _config(directory: Path) -> Path:
    return lowvol_config(
        directory,
        lowvol_market(TWO_REBALANCES),
        'holdout_start = "2022-09"\nholdout_months = 1\nwindows = 1\n'
        "out_of_sample_months = 1\nin_sample_months = 1\nmin_stitched_rebalances = 0\n"
        f'not_independent = "{NOT_INDEPENDENT}"\n',
    )


def _report(database_url: str, config: Path, protocol: str, directory: Path) -> dict[str, Any]:
    directory.mkdir()
    out = directory / "report.json"
    cli_ok(
        database_url,
        config,
        *("backtest", "report", "--protocol", protocol),
        *("--out", str(out), "--markdown", str(directory / "report.md")),
    )
    found: dict[str, Any] = json.loads(out.read_text())
    return found


def _load_to(conn: Any, fixtures: Path, start: date, end: date) -> None:
    ingest_eod(conn, PlaybackTransport(fixtures), start=start, end=end)
    compute_universe_months(conn, lowvol_market(TWO_REBALANCES), start, end)


Version = tuple[str, str, Path, Path, dict[str, Any]]


@pytest.fixture(scope="module")
def version(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Version:
    """``lowvol_v1`` frozen Not Independent on 2022-09-30, walked forward and run on the
    Holdout, its full report written before any Fresh Period: the database, the Protocol,
    the configuration, the fixtures and the report's JSON."""
    market = lowvol_market(TWO_REBALANCES)
    directory = tmp_path_factory.mktemp("not_independent")
    fixtures = write_fixtures(market, directory / "eod", CALENDAR)
    navs = daily_navs(CALENDAR, TWO_REBALANCES, "-0.01")
    amfi = write_amfi_fixtures(
        {BENCHMARK_SCHEME: navs, FACTOR_FUND: navs},
        directory / "amfi",
        first=date(2019, 9, 11),
        end=TWO_REBALANCES,
    )
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(fixtures), start=market.start, end=ONE_REBALANCE)
        ingest_corporate_actions(
            conn, PlaybackTransport(fixtures), start=market.start, end=market.end
        )
        assert market.declaration is not None
        declare_rule_validity(
            conn,
            market.rule.version,
            in_force_from=market.declaration.in_force_from,
            declared_at=market.declaration.declared_at,
            rule=market.rule,
        )
        compute_universe_months(conn, market, date(2022, 7, 1), ONE_REBALANCE)
        load_benchmark(conn, amfi, end=TWO_REBALANCES)
        load_index_history(conn)
        # The checked-in table's RELIANCE to JIOFIN lists inside this Market's span; nothing
        # holds RELIANCE, so the child's listing is never read.
        load_demerger_table(conn)
    config = _config(directory)
    cli_ok(database_url, config, "protocol", "freeze", "--strategy", "lowvol_v1")
    with app_connection(database_url) as conn:
        (frozen,) = queries.protocols(conn)
    with connect(database_url) as conn:
        conn.execute(
            "UPDATE protocols SET frozen_at = %s WHERE hash = %s",
            (datetime(2022, 9, 30, 16, tzinfo=IST), frozen.hash),
        )
    cli_ok(database_url, config, "backtest", "--protocol", frozen.hash)
    with app_connection(database_url) as conn:
        pass_holdout_checks(conn, frozen.hash)
    cli_ok(database_url, config, "backtest", "holdout", "--protocol", frozen.hash)
    report = _report(database_url, config, frozen.hash, directory / "holdout_alone")
    return database_url, frozen.hash, config, fixtures, report


def test_freezing_lowvol_v1_records_it_not_independent_with_its_reason(version: Version) -> None:
    url, protocol, _, _, report = version

    with app_connection(url) as conn:
        frozen = queries.protocol(conn, protocol)
        lineage = queries.lineage(conn, protocol)
        (attempt,) = queries.holdout_attempts(conn, protocol=protocol)

    assert frozen is not None and frozen.protocol.not_independent == NOT_INDEPENDENT
    (own,) = lineage.family.versions
    assert own.not_independent == NOT_INDEPENDENT
    # The family's first Holdout run consumes it, and is still Not Independent.
    assert (attempt.consumes, attempt.independent) == (True, False)
    assert attempt.not_independent == NOT_INDEPENDENT
    (listed,) = report["lineage"]["versions"]
    assert listed["not_independent"] == NOT_INDEPENDENT
    assert listed["holdout"]["independent"] is False


def test_the_gate_refuses_to_pass_on_the_holdout_alone(version: Version) -> None:
    gate = version[4]["gate"]

    assert gate["outcome"] == "cannot pass"
    assert gate["standing"]["independent"] is False
    assert NOT_INDEPENDENT in gate["reason"]
    assert version[4]["notes"]["not_independent"] == gate["standing"]["reason"]


Fresh = tuple[str, str, dict[str, Any], dict[str, Any]]


@pytest.fixture(scope="module")
def fresh(version: Version, tmp_path_factory: pytest.TempPathFactory) -> Fresh:
    """The Fresh Period run and reported with data to 2023-09-29 (one Rebalance after its
    Initial Buy), then extended to 2023-10-03 (two) and reported again: the database, the
    Protocol and the two reports' JSON."""
    url, protocol, config, fixtures, _ = version
    directory = tmp_path_factory.mktemp("fresh")
    cli_ok(url, config, "backtest", "fresh", "--protocol", protocol)
    one = _report(url, config, protocol, directory / "one")
    with app_connection(url) as conn:
        _load_to(conn, fixtures, TWO_REBALANCES, TWO_REBALANCES)
    cli_ok(url, config, "backtest", "fresh", "--protocol", protocol)
    two = _report(url, config, protocol, directory / "two")
    return url, protocol, one, two


def test_a_fresh_period_of_twelve_months_with_one_rebalance_cannot_carry_the_gate(
    fresh: Fresh,
) -> None:
    gate = fresh[2]["gate"]

    assert gate["standing"]["fresh_period"]["long_enough"] is True
    assert gate["fresh_period_rebalances"] == 1
    assert gate["outcome"] == "cannot pass"
    assert "1 Rebalance after its Initial Buy, short of 2" in gate["reason"]
    assert gate["thresholds"]["min_fresh_period_rebalances"] == 2


def test_with_two_rebalances_the_fresh_period_carries_the_gate(fresh: Fresh) -> None:
    gate = fresh[3]["gate"]

    assert gate["fresh_period_rebalances"] == 2
    checks = {c["metric"]: c["passed"] for c in gate["checks"] if c["curve"] == "fresh_period"}
    assert checks == {"annualised_excess": True, "drawdown": True, "rebalances": True}
    assert gate["outcome"] == "pass"


def test_the_initial_buy_is_never_counted_as_a_rebalance(fresh: Fresh) -> None:
    url, protocol, _, report = fresh

    with app_connection(url) as conn:
        run = queries.fresh_period_run(conn, protocol)
        (attempt,) = queries.holdout_attempts(conn, protocol=protocol)
        assert run is not None
        steps = {
            account: [
                (r.as_of, r.portfolio_step)
                for r in queries.runs(conn, mode="backtest", backtest_id=backtest_id)
                if r.portfolio_step is not None
            ]
            for account, backtest_id in (
                ("fresh_period", run.backtest_id),
                ("holdout", attempt.backtest_id),
            )
        }

    # The Fresh Period's Initial Buy is decided on a September's last trading day, as a
    # Rebalance would be, and is not one.
    assert steps["fresh_period"][0] == (FROZEN_ON, "initial_buy")
    rebalances = [d for d, step in steps["fresh_period"] if step == "rebalance"]
    assert rebalances == [date(2023, 3, 31), ONE_REBALANCE]
    assert report["gate"]["fresh_period_rebalances"] == 2
    # The Holdout's Initial Buy (decided 2022-08-30) and the Rebalance decided on its last
    # day count none.
    assert steps["holdout"][0] == (date(2022, 8, 30), "initial_buy")
    assert [d for d, step in steps["holdout"] if step == "rebalance"] == [FROZEN_ON]
    assert report["gate"]["holdout_rebalances"] == 0


def test_the_fresh_period_and_the_benchmark_start_with_the_initial_buy(fresh: Fresh) -> None:
    url, protocol, _, _ = fresh

    with app_connection(url) as conn:
        run = queries.fresh_period_run(conn, protocol)
        assert run is not None
        days = queries.liquidation_values(conn, mode="backtest", backtest_id=run.backtest_id)
        fills = queries.fills(conn, mode="backtest", backtest_id=run.backtest_id)

    assert run.first == FRESH_FIRST
    assert min(f.day for f in fills) == FRESH_FIRST
    # The Initial Buy's decision day has no Benchmark; it is bought the day the entrants are.
    assert (days[0].day, days[0].benchmark) == (FROZEN_ON, None)
    assert days[1].day == FRESH_FIRST
    assert days[1].benchmark is not None and days[1].benchmark.bought_on == FRESH_FIRST


def _fresh_command(url: str, config: Path, protocol: str) -> tuple[int, str]:
    return cli(url, config, "backtest", "fresh", "--protocol", protocol)


def test_the_fresh_period_is_a_backtest_and_needs_no_paper_machinery(fresh: Fresh) -> None:
    url, protocol, _, _ = fresh

    with app_connection(url) as conn:
        run = queries.fresh_period_run(conn, protocol)
        assert run is not None
        runs = queries.runs(conn, mode="backtest", backtest_id=run.backtest_id)
        paper = queries.runs(conn, mode="paper", backtest_id=None)

    assert runs[-1].as_of == TWO_REBALANCES
    assert paper == []


def test_the_fresh_period_is_refused_at_another_commit(
    fresh: Fresh, version: Version, clone_database: Callable[[str], str]
) -> None:
    url = clone_database(fresh[0])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(revision, "running_commit", lambda: "1a7e" * 10)
        code, out = _fresh_command(url, version[2], fresh[1])

    assert code == 1
    assert "a run under it must share its commit" in out


def test_the_fresh_period_is_refused_when_the_data_its_protocol_froze_has_changed(
    fresh: Fresh, version: Version, clone_database: Callable[[str], str], tmp_path: Path
) -> None:
    url = clone_database(fresh[0])
    table = tmp_path / "demergers.csv"
    table.write_text(
        "parent,child,ratio,ex_date,listed_on,parent_basis,child_basis,citation\n"
        "PARENT,CHILD,1:5,2020-06-01,2020-06-15,90,10,NSE/CMTR/00001\n"
    )
    with app_connection(url) as conn:
        ingest_demergers(conn, load_demergers(table))
        run = queries.fresh_period_run(conn, fresh[1])
        assert run is not None
        before = queries.runs(conn, mode="backtest", backtest_id=run.backtest_id)

    code, out = _fresh_command(url, version[2], fresh[1])

    assert code == 1
    assert "the demerger table" in out
    with app_connection(url) as conn:
        assert queries.runs(conn, mode="backtest", backtest_id=run.backtest_id) == before
