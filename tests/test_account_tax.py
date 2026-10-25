"""Tax in a backtest account, and Liquidation Value against the Benchmark (#96), end to end.

``lowvol_v1``'s Protocol keeps tax: frozen by ``tradeline protocol freeze --strategy
lowvol_v1`` with ``tax_rates.csv`` as of the freeze, and its one-cell Walk-Forward run
by ``tradeline backtest --protocol P``. Every figure is checked against the tax ledger's
pure functions (``tradeline.tax``, ``tradeline.benchmark``) over the account's events
as the query layer reads them.

The market is ``synthetic_eod.lowvol_market`` run on to 2023-05-31. The Out-of-Sample
Windows are the months 2022-04 to 2023-04, so the account starts 2022-04-01 (Initial
Buy decided 2022-03-31) and ends with a Boundary Exit on 2023-04-28. Its Rebalance of
2022-09-30 sells H15, H20 and H30 on 2022-10-03, H20 Rs 4,000 up on its cost: a large
short-term gain in financial year 2022, whose tax is debited on 2023-04-03, the first
trading day after 2023-03-31. The Rebalance of 2023-03-31 has no leaver. The Boundary
Exit settles financial year 2023.

The Benchmark's NAV is synthetic, recorded as AMFI's answers and loaded by ``ingest
benchmark``: 20 on 2022-03-01, rising 1 paisa a trading day.
"""

import contextlib
import io
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from synthetic_eod import (
    BENCHMARK_SCHEME,
    FACTOR_FUND,
    REPO_ROOT,
    compute_universe_months,
    config_text,
    load_benchmark,
    load_index_history,
    load_market,
    lowvol_market,
    repo_calendar,
    write_amfi_fixtures,
    write_fixtures,
)
from tradeline import queries, revision
from tradeline.benchmark import benchmark_liquidation
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.engine import TRADED_VALUE_DAYS, StoredMarketData
from tradeline.tax import Sale, liquidation_value, tax_ledger

D = Decimal
CALENDAR = repo_calendar()
END = date(2023, 5, 31)
FIRST_DAY = date(2022, 4, 1)
MARCH_31 = date(2023, 3, 31)
DEBIT_DAY = date(2023, 4, 3)
LAST_DAY = date(2023, 4, 28)
# A day holding thirty Positions, between the Rebalances.
HOLDING_DAY = date(2022, 12, 15)
STARTING_EQUITY = D(500000)

Frozen = tuple[str, queries.FrozenProtocol, dict[str, Any], uuid.UUID]


def _navs() -> dict[date, Decimal]:
    days = CALENDAR.trading_days(date(2022, 3, 1), END)
    return {day: D(20) + D("0.01") * i for i, day in enumerate(days)}


NAVS = _navs()


def _config(directory: Path) -> Path:
    config = directory / "tradeline.toml"
    config.write_text(
        config_text(lowvol_market(END))
        + f'[tax]\nschedule = "{REPO_ROOT / "tax_rates.csv"}"\n'
        + f'[benchmark]\nfunds = "{REPO_ROOT / "benchmark_funds.csv"}"\n'
        + '[lowvol.protocol]\nholdout_start = "2023-05"\nholdout_months = 1\nwindows = 13\n'
        "out_of_sample_months = 1\nin_sample_months = 1\ntax = true\n"
    )
    return config


def _cli(database_url: str, config: Path, *argv: str) -> None:
    with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", database_url)
        assert main(list(argv)) == 0


@pytest.fixture(scope="module")
def taxed(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Frozen:
    """The market and the Benchmark's NAVs loaded, ``lowvol_v1``'s Protocol frozen with tax
    and its Walk-Forward run: the database, the Protocol, the report's JSON and the
    Stitched Curve's backtest id."""
    market = lowvol_market(END)
    directory = tmp_path_factory.mktemp("taxed")
    fixtures = write_fixtures(market, directory / "eod", CALENDAR)
    amfi = write_amfi_fixtures(
        {BENCHMARK_SCHEME: NAVS, FACTOR_FUND: NAVS},
        directory / "amfi",
        first=date(2019, 9, 11),
        end=END,
    )
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, market)
        compute_universe_months(conn, market, date(2022, 3, 1), END)
        load_benchmark(conn, amfi, end=END)
        # The Relative Drawdown Halt's threshold is frozen from it.
        load_index_history(conn)
    config = _config(directory)
    _cli(database_url, config, "protocol", "freeze", "--strategy", "lowvol_v1")
    with app_connection(database_url) as conn:
        (found,) = queries.protocols(conn)
    out = directory / "walk_forward.json"
    _cli(database_url, config, "backtest", "--protocol", found.hash, "--out", str(out))
    with app_connection(database_url) as conn:
        walked = queries.walk_forward(conn, found.hash)
    assert walked is not None
    return database_url, found, json.loads(out.read_text()), walked.backtest_id


def _sales(database_url: str, backtest_id: uuid.UUID, *, before: date) -> list[Sale]:
    """Every Position the account sold before ``before``, as the tax ledger takes it: its
    buy's value and charges, against its sale's value less charges."""
    tags: dict[str, Any] = {"mode": "backtest", "backtest_id": backtest_id}
    with app_connection(database_url) as conn:
        positions = queries.positions(conn, **tags)
        fills = queries.fills(conn, **tags)
    sales = []
    for p in positions:
        if p.closed_on is None or p.closed_on >= before:
            continue
        own = [f for f in fills if f.position_id == p.id]
        sales.append(
            Sale(
                bought=p.opened_on,
                sold=p.closed_on,
                basis=-sum((f.cash_flow for f in own if f.side == "buy"), D(0)),
                proceeds=sum((f.cash_flow for f in own if f.side == "sell"), D(0)),
            )
        )
    return sales


def _runs(database_url: str, backtest_id: uuid.UUID) -> dict[date, queries.LedgerRun]:
    with app_connection(database_url) as conn:
        return {r.as_of: r for r in queries.runs(conn, mode="backtest", backtest_id=backtest_id)}


def _daily(report: dict[str, Any], day: date) -> dict[str, Any]:
    daily: list[dict[str, Any]] = report["stitched_curve"]["liquidation"]["daily"]
    (found,) = [d for d in daily if d["day"] == day.isoformat()]
    return found


def test_tax_is_debited_after_march_31_and_cash_falls_by_the_ledgers_amount(
    taxed: Frozen,
) -> None:
    database_url, frozen, report, backtest_id = taxed
    terms = frozen.protocol.tax
    assert terms is not None
    sales = _sales(database_url, backtest_id, before=DEBIT_DAY)
    (year,) = tax_ledger(sales, terms=terms, calendar=CALENDAR).years
    runs = _runs(database_url, backtest_id)
    with app_connection(database_url) as conn:
        fills = queries.fills(conn, mode="backtest", backtest_id=backtest_id)
    moved = sum((f.cash_flow for f in fills if f.day == DEBIT_DAY), D(0))

    # Financial year 2022 (April 2022 to March 2023): H20's sale alone gains about Rs 6 lakh.
    assert (year.year, year.debit_day) == (2022, DEBIT_DAY)
    assert year.tax > 100000
    before, after = runs[MARCH_31].cash, runs[DEBIT_DAY].cash
    assert before is not None and after is not None
    assert after == before + moved - year.tax
    debits = report["stitched_curve"]["liquidation"]["tax_debits"]
    assert debits[0] == {
        "financial_year": 2022,
        "day": DEBIT_DAY.isoformat(),
        "short_term_gain": str(year.short_term_gain.quantize(D("0.01"))),
        "long_term_gain": str(year.long_term_gain.quantize(D("0.01"))),
        "dividends": "0.00",
        "tax": str(year.tax),
        "settled": False,
    }


def test_the_reports_liquidation_value_on_the_last_day_is_the_ledgers_after_settling(
    taxed: Frozen,
) -> None:
    database_url, frozen, report, backtest_id = taxed
    terms = frozen.protocol.tax
    assert terms is not None
    sales = _sales(database_url, backtest_id, before=date(2099, 1, 1))
    with app_connection(database_url) as conn:
        fills = queries.fills(conn, mode="backtest", backtest_id=backtest_id)
    before_april = _sales(database_url, backtest_id, before=DEBIT_DAY)
    debited = tax_ledger(before_april, terms=terms, calendar=CALENDAR).years[0].tax
    cash = STARTING_EQUITY + sum((f.cash_flow for f in fills), D(0)) - debited

    found = liquidation_value(
        LAST_DAY, cash=cash, holdings=[], sales=sales, terms=terms, calendar=CALENDAR
    )

    liquidation = report["stitched_curve"]["liquidation"]
    assert _daily(report, LAST_DAY)["strategy"] == str(found.value)
    assert liquidation["value"] == str(found.value)
    # The Boundary Exit settles the final partial year: the account's cash is the value.
    settled = liquidation["tax_debits"][-1]
    assert (settled["financial_year"], settled["day"], settled["settled"]) == (
        2023,
        LAST_DAY.isoformat(),
        True,
    )
    assert settled["tax"] == str(found.tax_due)
    assert _runs(database_url, backtest_id)[LAST_DAY].cash == found.value


def test_unused_losses_at_liquidation_are_reported_with_their_size(taxed: Frozen) -> None:
    database_url, frozen, report, backtest_id = taxed
    terms = frozen.protocol.tax
    assert terms is not None
    sales = _sales(database_url, backtest_id, before=date(2099, 1, 1))

    dropped = tax_ledger(
        sales, terms=terms, calendar=CALENDAR, liquidated_on=LAST_DAY
    ).losses_dropped

    # The flat stocks sold at the end lose their round trip's costs.
    assert dropped.total > 0
    assert report["stitched_curve"]["liquidation"]["losses_dropped"] == {
        "short_term": str(dropped.short_term.quantize(D("0.01"))),
        "long_term": str(dropped.long_term.quantize(D("0.01"))),
    }


def test_the_liquidation_value_of_a_day_holding_positions_sells_them_at_the_close(
    taxed: Frozen,
) -> None:
    database_url, frozen, report, backtest_id = taxed
    terms = frozen.protocol.tax
    assert terms is not None
    costs = frozen.protocol.cost_model()
    tags: dict[str, Any] = {"mode": "backtest", "backtest_id": backtest_id}
    holdings = []
    with app_connection(database_url) as conn:
        data = StoredMarketData(conn, CALENDAR, as_of=HOLDING_DAY)
        fills = queries.fills(conn, **tags)
        for p in queries.positions(conn, **tags):
            if p.opened_on > HOLDING_DAY or (p.closed_on and p.closed_on <= HOLDING_DAY):
                continue
            (bar,) = queries.end_of_day(
                conn, p.symbol, start=HOLDING_DAY, end=HOLDING_DAY, as_of=HOLDING_DAY
            )
            assert bar.close is not None
            charges = costs.charges(
                "sell",
                day=HOLDING_DAY,
                price=bar.close,
                shares=p.shares,
                average_traded_value=data.average_traded_value(p.instrument, TRADED_VALUE_DAYS),
            )
            basis = -sum(
                (f.cash_flow for f in fills if f.position_id == p.id and f.side == "buy"), D(0)
            )
            proceeds = bar.close * p.shares - charges.total
            holdings.append(Sale(p.opened_on, HOLDING_DAY, basis, proceeds))
    cash = _runs(database_url, backtest_id)[HOLDING_DAY].cash
    assert cash is not None

    found = liquidation_value(
        HOLDING_DAY,
        cash=cash,
        holdings=holdings,
        sales=_sales(database_url, backtest_id, before=HOLDING_DAY + timedelta(days=1)),
        terms=terms,
        calendar=CALENDAR,
    )

    assert len(holdings) == 30
    assert _daily(report, HOLDING_DAY)["strategy"] == str(found.value)


def test_the_benchmark_is_bought_on_the_first_day_and_pays_its_exit_load_within_15_days(
    taxed: Frozen,
) -> None:
    _, frozen, report, _ = taxed
    terms, benchmark = frozen.protocol.tax, frozen.protocol.benchmark
    assert terms is not None and benchmark is not None
    day = date(2022, 4, 8)

    found = benchmark_liquidation(
        bought_on=FIRST_DAY,
        bought_nav=NAVS[FIRST_DAY],
        day=day,
        nav=NAVS[day],
        invested=STARTING_EQUITY,
        rates=benchmark.rates,
        terms=terms,
        calendar=CALENDAR,
    )

    assert found.sale.exit_load > 0
    assert _daily(report, day)["benchmark"] == str(found.liquidation.value)


def test_the_benchmark_pays_stamp_duty_stt_and_says_which_rate_its_gain_paid(
    taxed: Frozen,
) -> None:
    _, frozen, report, _ = taxed
    terms, benchmark = frozen.protocol.tax, frozen.protocol.benchmark
    assert terms is not None and benchmark is not None

    found = benchmark_liquidation(
        bought_on=FIRST_DAY,
        bought_nav=NAVS[FIRST_DAY],
        day=LAST_DAY,
        nav=NAVS[LAST_DAY],
        invested=STARTING_EQUITY,
        rates=benchmark.rates,
        terms=terms,
        calendar=CALENDAR,
    )

    reported = report["stitched_curve"]["liquidation"]["benchmark"]
    # Held 2022-04-01 to 2023-04-28, over twelve months: long-term, 12.5 percent plus cess.
    assert reported == {
        "scheme_code": BENCHMARK_SCHEME,
        "bought_on": FIRST_DAY.isoformat(),
        "invested": "500000.00",
        "bought_nav": str(NAVS[FIRST_DAY]),
        "nav": str(NAVS[LAST_DAY]),
        "stamp_duty": "25.00",
        "stt": str(found.sale.stt),
        "exit_load": "0.00",
        "taxed_as": "long_term",
        "tax_rate": "0.13",
        "tax_due": str(found.liquidation.tax_due),
        "value": str(found.liquidation.value),
    }
    assert _daily(report, LAST_DAY)["benchmark"] == str(found.liquidation.value)


def test_a_drift_replay_at_a_later_commit_reproduces_the_tax_ledger(
    taxed: Frozen, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, found, _, backtest_id = taxed
    monkeypatch.setattr(revision, "running_commit", lambda: "1a7e" * 10)

    _cli(database_url, _config(tmp_path), "backtest", "drift", "--protocol", found.hash)

    with app_connection(database_url) as conn:
        (replay,) = queries.drift_replays(conn, protocol=found.hash)
        replayed = replay.accounts["stitched_curve"]
        tags: dict[str, Any] = {"mode": "backtest"}
        frozen = queries.tax_debits(conn, **tags, backtest_id=backtest_id)
        again = queries.tax_debits(conn, **tags, backtest_id=replayed)
        days = [
            len(queries.liquidation_values(conn, **tags, backtest_id=account))
            for account in (backtest_id, replayed)
        ]
    assert (replay.outcome, replay.differences) == ("pass", [])
    # Financial year 2022's debit and the Boundary Exit's settlement of 2023, both again.
    assert [(d.day, d.settled) for d in frozen] == [(DEBIT_DAY, False), (LAST_DAY, True)]
    assert again == frozen
    assert days[0] == days[1] > 0
