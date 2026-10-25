"""The pre-Holdout checks (ADR 0007, 2026-10-25 amendment; #102): the machinery checked from
records, not results, before the one-shot Holdout runs.

The market is ``synthetic_eod.walk_forward_market`` (Protocol range 2022-04 to the
Holdout, 2023-01; ASH, BIRCH and CEDAR members of every month) with actions NSE lists:

- ASH, 2022-08-01, "Dividend - Rs 5 Per Share": parsed.
- BIRCH, 2022-11-15, "Interim Dividend": no amount, so unparsed.
- CEDAR, 2022-12-05, "Demerger", with no demerger table row until a test stores one.
- DUSK, listed 2023-01-02 and so a member of no month (its listing age is short of 20
  trading days through the Holdout), 2023-01-16, "Dividend": unparsed, but no member's.
- BIRCH, 2021-06-01, "Dividend" before the Protocol's range: no decision date reads it.

``tradeline backtest checks --protocol P`` runs the checks and records each;
``tradeline backtest holdout`` refuses until each is recorded as passed at the commit it
runs at. Assertions go through ``tradeline.queries`` and the services' results.
"""

import contextlib
import io
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from checkout import TEST_COMMIT
from synthetic_eod import (
    REPO_ROOT,
    WALK_FORWARD_RANGE,
    Absent,
    Demerger,
    Dividend,
    Stock,
    config_text,
    freeze_protocol,
    load_market,
    repo_calendar,
    walk_forward_market,
    walk_forward_terms,
    write_fixtures,
)
from test_holdout import CALENDAR, LIMITS, TICK_SIZES, holdout, pullback
from tradeline import queries, revision
from tradeline.cli import main
from tradeline.costs import ContractNote, load_cost_schedule
from tradeline.db import app_connection
from tradeline.demergers import load_demergers
from tradeline.dividends import ListedDividend, load_listed_dividends
from tradeline.holdout_checks import (
    PreHoldoutChecksUnmet,
    TaxTestOutcome,
    check_holdout,
    eligible_members,
)
from tradeline.ingest.demergers import ingest_demergers
from tradeline.walk_forward import walk_forward

D = Decimal
SCHEDULE = load_cost_schedule(REPO_ROOT / "cost_rates.csv")
_BASE = walk_forward_market()
MARKET = (
    _BASE.with_events("ASH", Dividend(date(2022, 8, 1), "Dividend - Rs 5 Per Share"))
    .with_events(
        "BIRCH",
        Dividend(date(2022, 11, 15), "Interim Dividend"),
        Dividend(date(2021, 6, 1), "Dividend"),
    )
    .with_events("CEDAR", Demerger(date(2022, 12, 5)))
)
MARKET = replace(
    MARKET,
    stocks=(
        *MARKET.stocks,
        Stock(
            "DUSK",
            "INE000D01010",
            start=D("100.00"),
            slope=D("0.50"),
            half_range=D("1.00"),
            volume=100_000,
            listed=date(2023, 1, 2),
            events=(
                Absent(MARKET.start, date(2023, 1, 1)),
                Dividend(date(2023, 1, 16), "Dividend"),
            ),
        ),
    ),
)
UNPARSED = ListedDividend(
    "BIRCH", date(2022, 11, 15), "Interim Dividend", "no amount in NSE's subject", 2
)
NOTE = ContractNote(
    trade_date=date(2025, 1, 6),
    side="buy",
    price=D("1234.50"),
    shares=100,
    stt=D("123"),
    stamp_duty=D("19"),
    exchange_charge=D("3.67"),
    sebi_fee=D("0.12"),
    gst=D("0.68"),
    source="CN 1 of 2025-01-06",
)
DEMERGERS = (
    "parent,child,ratio,ex_date,listed_on,parent_basis,child_basis,citation\n"
    "CEDAR,CEDARNEW,1:10,2022-12-05,2022-12-20,90,10,NSE/CMTR/1\n"
)


def passing_tax_tests() -> TaxTestOutcome:
    return TaxTestOutcome(passed=True, summary="32 passed")


def failing_tax_tests() -> TaxTestOutcome:
    return TaxTestOutcome(passed=False, summary="1 failed, 31 passed")


def checks(conn: psycopg.Connection, protocol: str, **changes: Any) -> Any:
    """The checks under Protocol ``protocol``, each passing unless ``changes`` say otherwise:
    no contract note, accepted; BIRCH's subject listed; the passing tax tests."""
    arguments: dict[str, Any] = {
        "contract_notes": (),
        "schedule": SCHEDULE,
        "listed": (UNPARSED,),
        "accept_no_contract_notes": "no Zerodha account has traded yet",
        "tax_tests": passing_tax_tests,
        **changes,
    }
    return check_holdout(conn, protocol=protocol, **arguments)


@pytest.fixture(scope="module")
def frozen(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """The market loaded and a Protocol frozen over it, nothing run: the database and its
    hash."""
    fixtures = write_fixtures(MARKET, tmp_path_factory.mktemp("checks"), repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        found = freeze_protocol(
            conn, MARKET, *WALK_FORWARD_RANGE, terms=walk_forward_terms(), limits=LIMITS
        )
    return database_url, found.hash


@pytest.fixture(scope="module")
def walked_frozen(frozen: tuple[str, str], clone_database: Callable[[str], str]) -> tuple[str, str]:
    """A copy of ``frozen`` with the Protocol's Walk-Forward finished, its Holdout not run."""
    source, protocol = frozen
    url = clone_database(source)
    with app_connection(url) as conn:
        walk_forward(conn, pullback, protocol=protocol, calendar=CALENDAR, tick_sizes=TICK_SIZES)
    return url, protocol


@pytest.fixture
def fresh(frozen: tuple[str, str], clone_database: Callable[[str], str]) -> tuple[str, str]:
    source, protocol = frozen
    return clone_database(source), protocol


@pytest.fixture
def ready(walked_frozen: tuple[str, str], clone_database: Callable[[str], str]) -> tuple[str, str]:
    source, protocol = walked_frozen
    return clone_database(source), protocol


def test_the_holdout_is_refused_naming_each_check_not_recorded_as_passed(
    ready: tuple[str, str],
) -> None:
    url, protocol = ready
    with app_connection(url) as conn:
        with pytest.raises(PreHoldoutChecksUnmet) as refused:
            holdout(conn, protocol)

        for item in (
            "the contract-note test",
            "the dividend subjects",
            "the demerger table",
            "the tax-ledger tests",
        ):
            assert item in str(refused.value)
        assert "not run at commit" in str(refused.value)
        assert queries.holdout_attempts(conn, protocol=protocol) == []


def test_each_unmet_check_is_recorded_with_its_commit_and_refuses_the_holdout_by_name(
    ready: tuple[str, str],
) -> None:
    url, protocol = ready
    with app_connection(url) as conn:
        result = checks(
            conn,
            protocol,
            listed=(),
            accept_no_contract_notes=None,
            tax_tests=failing_tax_tests,
        )

        assert not result.passed
        recorded = queries.holdout_checks(conn, protocol=protocol)
        assert {(c.item, c.passed, c.git_commit) for c in recorded} == {
            ("contract_notes", False, TEST_COMMIT),
            ("dividends", False, TEST_COMMIT),
            ("demergers", False, TEST_COMMIT),
            ("tax_tests", False, TEST_COMMIT),
        }
        with pytest.raises(PreHoldoutChecksUnmet) as refused:
            holdout(conn, protocol)
        message = str(refused.value)
        assert "the contract-note test (no contract note supplied" in message
        assert "BIRCH 2022-11-15 'Interim Dividend'" in message
        assert "CEDAR 2022-12-05 'Demerger'" in message
        assert "1 failed, 31 passed" in message
        assert queries.holdout_attempts(conn, protocol=protocol) == []


def test_the_eligible_members_come_from_the_universe_records_with_no_strategy_run(
    fresh: tuple[str, str],
) -> None:
    url, protocol = fresh
    with app_connection(url) as conn:
        frozen = queries.protocol(conn, protocol)
        assert frozen is not None

        assert eligible_members(conn, frozen) == {"ASH", "BIRCH", "CEDAR"}

        result = checks(conn, protocol, listed=())
        (dividends,) = (c for c in result.checks if c.item == "dividends")
        assert not dividends.passed
        assert (dividends.detail["parsed"], dividends.detail["unparsed"]) == (1, 1)
        assert [s["symbol"] for s in dividends.detail["unlisted"]] == ["BIRCH"]
        assert queries.runs(conn, mode="backtest") == []


def test_a_demerger_in_nses_list_for_an_eligible_member_with_no_table_row_fails(
    fresh: tuple[str, str], tmp_path: Path
) -> None:
    url, protocol = fresh
    with app_connection(url) as conn:
        result = checks(conn, protocol)
        (demergers,) = (c for c in result.checks if c.item == "demergers")
        assert not demergers.passed
        assert demergers.detail["missing"] == [
            {"symbol": "CEDAR", "ex_date": "2022-12-05", "subject": "Demerger"}
        ]

        (tmp_path / "demergers.csv").write_text(DEMERGERS)
        ingest_demergers(conn, load_demergers(tmp_path / "demergers.csv"))
        result = checks(conn, protocol)

        assert result.passed
        (demergers,) = (c for c in result.checks if c.item == "demergers")
        assert demergers.detail == {"listed": 1, "missing": []}


def test_a_contract_note_off_the_rates_fails_even_when_none_would_be_accepted(
    fresh: tuple[str, str],
) -> None:
    url, protocol = fresh
    wrong = replace(NOTE, stt=D("124"))
    with app_connection(url) as conn:
        right = checks(conn, protocol, contract_notes=(NOTE,), accept_no_contract_notes=None)
        off = checks(conn, protocol, contract_notes=(wrong,))

    (note,) = (c for c in right.checks if c.item == "contract_notes")
    assert (note.passed, note.accepted) == (True, None)
    (note,) = (c for c in off.checks if c.item == "contract_notes")
    assert not note.passed
    assert "CN 1 of 2025-01-06" in note.summary


def test_the_holdout_runs_once_every_check_has_passed_at_its_commit(
    ready: tuple[str, str], tmp_path: Path
) -> None:
    url, protocol = ready
    (tmp_path / "demergers.csv").write_text(DEMERGERS)
    with app_connection(url) as conn:
        ingest_demergers(conn, load_demergers(tmp_path / "demergers.csv"))
        checks(conn, protocol, tax_tests=failing_tax_tests)
        result = checks(conn, protocol)
        assert result.passed
        recorded = queries.holdout_checks(conn, protocol=protocol)[-4:]
        (note,) = (c for c in recorded if c.item == "contract_notes")
        assert note.accepted == "no Zerodha account has traded yet"

        ran = holdout(conn, protocol)

        assert ran.attempt.outcome == "ran"


def test_checks_passed_at_another_commit_do_not_open_the_holdout(
    ready: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, protocol = ready
    (tmp_path / "demergers.csv").write_text(DEMERGERS)
    with app_connection(url) as conn:
        ingest_demergers(conn, load_demergers(tmp_path / "demergers.csv"))
        monkeypatch.setattr(revision, "running_commit", lambda: "0" * 40)
        assert checks(conn, protocol).passed
        monkeypatch.setattr(revision, "running_commit", lambda: TEST_COMMIT)

        with pytest.raises(PreHoldoutChecksUnmet, match=f"not run at commit {TEST_COMMIT[:12]}"):
            holdout(conn, protocol)


def test_the_real_tax_ledger_tests_pass_at_this_checkout(fresh: tuple[str, str]) -> None:
    url, protocol = fresh
    with app_connection(url) as conn:
        result = check_holdout(
            conn,
            protocol=protocol,
            contract_notes=(),
            schedule=SCHEDULE,
            listed=(UNPARSED,),
            accept_no_contract_notes="none yet",
        )

    (tax,) = (c for c in result.checks if c.item == "tax_tests")
    assert tax.passed, tax.summary
    assert "passed" in tax.summary


def test_the_checks_command_records_the_checks_and_fails_on_an_unmet_one(
    fresh: tuple[str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url, protocol = fresh
    config = tmp_path / "tradeline.toml"
    listed = tmp_path / "unparsed_dividends.csv"
    listed.write_text(
        "symbol,ex_date,subject,reason\nBIRCH,2022-11-15,Interim Dividend,no amount stated\n"
    )
    config.write_text(
        config_text(MARKET) + f'[corporate_actions]\nunparsed_dividends = "{listed}"\n'
    )
    with (
        pytest.MonkeyPatch.context() as patch,
        contextlib.redirect_stdout(io.StringIO()),
        caplog.at_level(logging.INFO),
    ):
        patch.setenv("TRADELINE_CONFIG", str(config))
        patch.setenv("DATABASE_URL", url)
        patch.setattr("tradeline.holdout_checks.run_tax_tests", passing_tax_tests)
        code = main(
            [
                "backtest",
                "checks",
                "--protocol",
                protocol,
                "--accept-no-contract-notes",
                "no Zerodha account has traded yet",
            ]
        )

    assert code == 1
    with app_connection(url) as conn:
        recorded = {c.item: c.passed for c in queries.holdout_checks(conn, protocol=protocol)}
    assert recorded == {
        "contract_notes": True,
        "dividends": True,
        "demergers": False,
        "tax_tests": True,
    }


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("BIRCH,2022-11-15,Interim Dividend,", "no reason"),
        ("BIRCH,2022-11-15,Interim Dividend,a\nBIRCH,2022-11-15,Interim Dividend,b", "already"),
        ("BIRCH,15-11-2022,Interim Dividend,a", "is not a date"),
    ],
)
def test_a_bad_row_refuses_the_whole_unparsed_dividend_list(
    tmp_path: Path, row: str, message: str
) -> None:
    path = tmp_path / "unparsed_dividends.csv"
    path.write_text(f"symbol,ex_date,subject,reason\n{row}\n")

    with pytest.raises(ValueError, match=message):
        load_listed_dividends(path)


def test_the_checked_in_unparsed_dividend_list_loads() -> None:
    assert load_listed_dividends(REPO_ROOT / "unparsed_dividends.csv") == ()
