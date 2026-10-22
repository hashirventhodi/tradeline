"""``reconcile``: Kite's daily candles against NSE's end-of-day files, per symbol per day.

Each test loads its own database from the recorded fixtures: the master and
end-of-day rows (``ingest_eod``), NSE's corporate actions, and Kite's candles
for the four ``EQ`` symbols over 2024-07-01 to 2024-08-30. Kite's bars are
adjusted by Kite's rules (RELIANCE halved by a later bonus, INFY and
BAJAJ-AUTO scaled by about 0.98 for large dividends), so the clean fixtures
already differ from NSE's closes by a factor per symbol that reconcile must
not report.

A discrepancy is injected by playing the real recorded Kite responses back
with one bar dropped or one close changed on its way to the ingest
(``playback``), so no fixture file is edited and none is written by hand.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from playback import (
    Candle,
    CandleEdit,
    Playback,
    drop_bar,
    kite_candles,
    shift_close,
)
from tradeline.cli import main
from tradeline.config import Config
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.reconcile import Discrepancy, ReconcileReport, reconcile
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport, Request

FIXTURES = Path(__file__).parent / "fixtures"
START, END = date(2024, 7, 1), date(2024, 8, 30)
ACTIONS_START, ACTIONS_END = date(2024, 7, 1), date(2024, 12, 31)
TRADING_DAYS = 43
EQ_SYMBOLS = 4
ROWS = TRADING_DAYS * EQ_SYMBOLS
SETTINGS = Config.Reconcile(close_tolerance=0.001, max_discrepancy_pct=0.1)

INFY_TOKEN = "408065"


def untouched(request: Request, candles: list[Candle]) -> list[Candle]:
    return candles


def load(url: str, edit: CandleEdit = untouched) -> None:
    with app_connection(url) as conn:
        ingest_eod(conn, PlaybackTransport(FIXTURES / "ingest_eod"), start=START, end=END)
        ingest_corporate_actions(
            conn,
            PlaybackTransport(FIXTURES / "ingest_corporate_actions"),
            start=ACTIONS_START,
            end=ACTIONS_END,
        )
        transport = Playback(FIXTURES / "ingest_candles", edit=kite_candles(edit))
        session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
        ingest_candles(conn, session, start=START, end=END)


def run(url: str, settings: Config.Reconcile = SETTINGS) -> ReconcileReport:
    with app_connection(url) as conn:
        return reconcile(conn, settings, start=START, end=END)


def test_clean_fixtures_reconcile_fully_with_no_discrepancies(fresh_database_url: str) -> None:
    load(fresh_database_url)

    report = run(fresh_database_url)

    assert report.rows == ROWS
    assert report.missing == ()
    assert report.extra == ()
    assert report.close_mismatches == ()
    assert report.discrepancies == 0
    assert report.reconciliation_pct == Decimal(100)
    assert report.passed


def test_a_deleted_candle_day_is_reported_as_missing_for_that_symbol_and_date_alone(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    report = run(fresh_database_url)

    assert report.missing == (
        Discrepancy(
            kind="missing",
            symbol="INFY",
            series="EQ",
            trade_date=date(2024, 7, 15),
            candle_close=None,
            eod_close=Decimal("1707.05"),  # NSE's close in the 15 July 2024 file,
        ),
    )
    assert report.extra == ()
    assert report.close_mismatches == ()
    assert report.rows == ROWS
    assert report.discrepancies == 1


def test_a_close_altered_beyond_tolerance_is_reported_for_that_row_alone(
    fresh_database_url: str,
) -> None:
    # 1557.1 to 1573.1: about 1 percent, ten times the tolerance.
    load(fresh_database_url, shift_close(INFY_TOKEN, "2024-07-01", "16"))

    report = run(fresh_database_url)

    ((mismatch),) = report.close_mismatches
    assert (mismatch.symbol, mismatch.series, mismatch.trade_date) == (
        "INFY",
        "EQ",
        date(2024, 7, 1),
    )
    assert mismatch.candle_close == Decimal("1573.1")
    assert mismatch.eod_close == Decimal("1590.8")  # NSE's close in the 1 July 2024 file
    # NSE's close times INFY's Kite factor of about 0.9788.
    assert Decimal("1557.0") < mismatch.expected_close < Decimal("1557.2")  # type: ignore[operator]
    assert report.missing == report.extra == ()


def test_a_close_altered_within_tolerance_is_not_reported(fresh_database_url: str) -> None:
    # 1557.1 to 1558.1: about 0.06 percent, inside the 0.1 percent tolerance.
    load(fresh_database_url, shift_close(INFY_TOKEN, "2024-07-01", "1"))

    report = run(fresh_database_url)

    assert report.close_mismatches == ()
    assert report.discrepancies == 0


def test_the_run_fails_when_discrepancies_exceed_the_threshold_and_passes_under_it(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    strict = run(fresh_database_url, Config.Reconcile(max_discrepancy_pct=0.1))
    lenient = run(fresh_database_url, Config.Reconcile(max_discrepancy_pct=1.0))

    # One row in 172 is 0.58 percent.
    assert strict.discrepancy_pct == lenient.discrepancy_pct
    assert Decimal("0.58") < strict.discrepancy_pct < Decimal("0.59")
    assert Decimal("99.41") < strict.reconciliation_pct < Decimal("99.42")
    assert not strict.passed
    assert lenient.passed


def test_a_run_exactly_at_the_threshold_fails_since_it_must_be_under_it(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    with app_connection(fresh_database_url) as conn:
        # Monday 15 to Friday 19 July less Wednesday's Muharram holiday: four
        # days of four symbols, one row missing, so 6.25 percent.
        report = reconcile(
            conn,
            Config.Reconcile(max_discrepancy_pct=6.25),
            start=date(2024, 7, 15),
            end=date(2024, 7, 19),
        )

    assert report.rows == 16
    assert report.discrepancy_pct == Decimal("6.25")
    assert not report.passed


def test_an_empty_database_cannot_pass(fresh_database_url: str) -> None:
    with pytest.raises(ValueError, match="no candles or end-of-day rows"):
        run(fresh_database_url)


def test_symbols_can_be_narrowed_and_one_without_candles_is_refused(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    with app_connection(fresh_database_url) as conn:
        tcs = reconcile(conn, SETTINGS, start=START, end=END, symbols=["TCS"])
        with pytest.raises(ValueError, match="KHADIM"):
            reconcile(conn, SETTINGS, start=START, end=END, symbols=["TCS", "KHADIM"])

    assert tcs.rows == TRADING_DAYS
    assert tcs.discrepancies == 0


REPO_CONFIG = Path(__file__).resolve().parents[1] / "tradeline.toml"


def reconcile_command(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[dict[str, Any]], str]:
    monkeypatch.setenv("DATABASE_URL", url)
    code = main(["reconcile", "--from", START.isoformat(), "--to", END.isoformat()])
    captured = capsys.readouterr()
    return code, [json.loads(line) for line in captured.out.splitlines()], captured.err


def test_the_command_exits_zero_on_clean_fixtures_with_a_summary_and_a_table(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_CONFIG))
    load(fresh_database_url)

    code, lines, table = reconcile_command(fresh_database_url, monkeypatch, capsys)

    assert code == 0
    assert all(
        (line["range_start"], line["range_end"]) == ("2024-07-01", "2024-08-30") for line in lines
    )
    summary = lines[-1]
    assert summary["command"] == "reconcile"
    assert summary["msg"] == "reconcile finished"
    assert summary["report"] == {
        "start": "2024-07-01",
        "end": "2024-08-30",
        "close_tolerance": "0.001",
        "max_discrepancy_pct": "0.1",
        "rows": ROWS,
        "discrepancies": 0,
        "missing": 0,
        "extra": 0,
        "close_mismatches": 0,
        "discrepancy_pct": "0.0000",
        "reconciliation_pct": "100.0000",
        "passed": True,
        "listed": [],
        "candle_disagreements": 0,
        "listed_candle_disagreements": [],
        "pre_listing_candles": 0,
        "listed_pre_listing_candles": [],
    }
    assert "172 rows, 0 discrepancies" in table
    assert "PASS" in table


def test_the_command_exits_non_zero_above_the_configured_threshold(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))
    config = tmp_path / "tradeline.toml"

    config.write_text("[reconcile]\nmax_discrepancy_pct = 0.1\n")
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))
    strict_code, strict_lines, strict_table = reconcile_command(
        fresh_database_url, monkeypatch, capsys
    )
    config.write_text("[reconcile]\nmax_discrepancy_pct = 1.0\n")
    lenient_code, _, _ = reconcile_command(fresh_database_url, monkeypatch, capsys)

    assert strict_code == 1
    assert lenient_code == 0
    report = strict_lines[-1]["report"]
    assert report["passed"] is False
    assert report["listed"] == [
        {
            "kind": "missing",
            "symbol": "INFY",
            "series": "EQ",
            "trade_date": "2024-07-15",
            "candle_close": None,
            "eod_close": "1707.05",
            "expected_close": None,
        }
    ]
    assert "FAIL" in strict_table
    assert "missing  INFY" in strict_table


def test_every_line_of_the_command_carries_its_range_and_the_symbols_named(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_CONFIG))
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    load(fresh_database_url)
    capsys.readouterr()

    code = main(["reconcile", "--from", "2024-07-01", "--to", "2024-07-31", "--symbol", "INFY"])

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert lines
    for line in lines:
        assert (line["range_start"], line["range_end"]) == ("2024-07-01", "2024-07-31")
        assert line["symbols"] == ["INFY"]
