"""Kite Parity: Kite's daily candles against NSE's end-of-day files, per symbol per day.

Each test loads its own database from the recorded fixtures: the master and
end-of-day rows (``ingest_eod``), NSE's corporate actions, and Kite's candles
for the four ``EQ`` symbols over 2024-07-01 to 2024-08-30. Kite's bars are
adjusted by Kite's rules (RELIANCE halved by a later bonus, INFY and
BAJAJ-AUTO scaled by about 0.98 for large dividends), so the clean fixtures
already differ from NSE's closes by a factor per symbol that parity must
not report.

A difference is injected by playing the real recorded Kite responses back
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
    Edit,
    Playback,
    affine_prices,
    bhavcopy_series,
    drop_bar,
    kite_candles,
    shift_close,
    then,
    whole_rupees,
    zero_prices,
)
from tradeline.cli import main
from tradeline.config import Config
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.parity import Difference, ParityReport, Reason, parity
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport, Request

FIXTURES = Path(__file__).parent / "fixtures"
START, END = date(2024, 7, 1), date(2024, 8, 30)
ACTIONS_START, ACTIONS_END = date(2024, 7, 1), date(2024, 12, 31)
TRADING_DAYS = 43
EQ_SYMBOLS = 4
ROWS = TRADING_DAYS * EQ_SYMBOLS
SETTINGS = Config.Parity(close_tolerance=0.001, max_unexplained_pct=0.1)

INFY_TOKEN = "408065"


def untouched(request: Request, candles: list[Candle]) -> list[Candle]:
    return candles


def load(url: str, edit: CandleEdit = untouched, eod: Edit | None = None) -> None:
    with app_connection(url) as conn:
        ingest_eod(conn, Playback(FIXTURES / "ingest_eod", edit=eod), start=START, end=END)
        ingest_corporate_actions(
            conn,
            PlaybackTransport(FIXTURES / "ingest_corporate_actions"),
            start=ACTIONS_START,
            end=ACTIONS_END,
        )
        transport = Playback(FIXTURES / "ingest_candles", edit=kite_candles(edit))
        session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
        ingest_candles(conn, session, start=START, end=END)


def run(url: str, settings: Config.Parity = SETTINGS) -> ParityReport:
    with app_connection(url) as conn:
        return parity(conn, settings, start=START, end=END)


def test_clean_fixtures_are_at_parity_with_no_differences(fresh_database_url: str) -> None:
    load(fresh_database_url)

    report = run(fresh_database_url)

    assert report.rows == ROWS
    assert report.missing == ()
    assert report.extra == ()
    assert report.close_mismatches == ()
    assert report.differences == 0
    assert report.unexplained == ()
    assert report.unexplained_pct == Decimal(0)
    assert report.passed


def test_a_deleted_candle_day_is_reported_as_missing_for_that_symbol_and_date_alone(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    report = run(fresh_database_url)

    assert report.missing == (
        Difference(
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
    assert report.differences == 1


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
    assert report.differences == 0


def test_the_run_fails_when_unexplained_differences_exceed_the_threshold_and_passes_under_it(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    strict = run(fresh_database_url, Config.Parity(max_unexplained_pct=0.1))
    lenient = run(fresh_database_url, Config.Parity(max_unexplained_pct=1.0))

    # One unexplained row in 172 is 0.58 percent.
    assert strict.unexplained_pct == lenient.unexplained_pct
    assert Decimal("0.58") < strict.unexplained_pct < Decimal("0.59")
    assert not strict.passed
    assert lenient.passed


def test_a_run_exactly_at_the_threshold_fails_since_it_must_be_under_it(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    with app_connection(fresh_database_url) as conn:
        # Monday 15 to Friday 19 July less Wednesday's Muharram holiday: four
        # days of four symbols, one row missing, so 6.25 percent.
        report = parity(
            conn,
            Config.Parity(max_unexplained_pct=6.25),
            start=date(2024, 7, 15),
            end=date(2024, 7, 19),
        )

    assert report.rows == 16
    assert report.unexplained_pct == Decimal("6.25")
    assert not report.passed


def test_with_no_parity_reason_yet_every_difference_is_unexplained_and_counted_by_kind(
    fresh_database_url: str,
) -> None:
    load(
        fresh_database_url,
        then(drop_bar(INFY_TOKEN, "2024-07-15"), shift_close(INFY_TOKEN, "2024-07-01", "16")),
    )

    report = run(fresh_database_url)

    assert [(d.kind, d.trade_date, d.reason) for d in report.unexplained] == [
        ("missing", date(2024, 7, 15), None),
        ("close", date(2024, 7, 1), None),
    ]
    assert report.differences == 2
    summary = report.summary()
    assert (summary["differences"], summary["unexplained"]) == (2, 2)
    assert summary["by_kind"] == {
        "missing": {"total": 1, "unexplained": 1},
        "extra": {"total": 0, "series_move": 0, "unexplained": 0},
        "close": {
            "total": 1,
            "additive_adjustment": 0,
            "coarse_rounding": 0,
            "unexplained": 1,
        },
    }
    assert summary["by_reason"] == {
        "series_move": 0,
        "additive_adjustment": 0,
        "coarse_rounding": 0,
        "unexplained": 2,
    }
    assert [(d["kind"], d["reason"]) for d in summary["listed_unexplained"]] == [
        ("missing", None),
        ("close", None),
    ]


def test_a_candle_on_a_day_nse_lists_the_instrument_only_in_be_is_a_series_move(
    fresh_database_url: str,
) -> None:
    # NSE lists INFY in BE on 15 July 2024; Kite's EQ token carries on that day.
    load(fresh_database_url, eod=bhavcopy_series(date(2024, 7, 15), "INFY", "BE"))

    report = run(fresh_database_url)

    assert report.extra == (
        Difference(
            kind="extra",
            symbol="INFY",
            series="EQ",
            trade_date=date(2024, 7, 15),
            candle_close=Decimal("1670.9"),
            eod_close=None,
            reason=Reason.SERIES_MOVE,
        ),
    )
    assert report.unexplained == ()
    assert report.passed
    summary = report.summary()
    assert summary["by_kind"]["extra"] == {"total": 1, "series_move": 1, "unexplained": 0}
    assert summary["by_reason"]["series_move"] == 1


def test_a_candle_on_a_day_nse_lists_the_instrument_in_no_series_stays_unexplained(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, eod=bhavcopy_series(date(2024, 7, 15), "INFY", None))

    report = run(fresh_database_url)

    assert [(d.kind, d.symbol, d.trade_date, d.reason) for d in report.unexplained] == [
        ("extra", "INFY", date(2024, 7, 15), None)
    ]
    assert (report.by_reason["series_move"], report.by_reason["unexplained"]) == (0, 1)
    assert not report.passed


def test_an_empty_database_cannot_pass(fresh_database_url: str) -> None:
    with pytest.raises(ValueError, match="no candles or end-of-day rows"):
        run(fresh_database_url)


def test_symbols_can_be_narrowed_and_one_without_candles_is_refused(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    with app_connection(fresh_database_url) as conn:
        tcs = parity(conn, SETTINGS, start=START, end=END, symbols=["TCS"])
        with pytest.raises(ValueError, match="KHADIM"):
            parity(conn, SETTINGS, start=START, end=END, symbols=["TCS", "KHADIM"])

    assert tcs.rows == TRADING_DAYS
    assert tcs.differences == 0


# Monday 1 and Tuesday 2 July 2024: one stretch of two INFY bars.
SHORT_START, SHORT_END = date(2024, 7, 1), date(2024, 7, 2)


def run_short(url: str) -> ParityReport:
    with app_connection(url) as conn:
        return parity(conn, SETTINGS, start=SHORT_START, end=SHORT_END, symbols=["INFY"])


def test_a_zero_close_is_left_out_of_its_stretchs_factor_and_compared_against_the_rest(
    fresh_database_url: str,
) -> None:
    # Of two bars, a ratio of 0 in the median would halve the factor and fail both.
    load(fresh_database_url, zero_prices(INFY_TOKEN, "2024-07-01"))

    report = run_short(fresh_database_url)

    ((mismatch),) = report.close_mismatches
    assert mismatch.trade_date == date(2024, 7, 1)
    assert mismatch.candle_close == Decimal(0)
    # NSE's 1590.8 times the factor of 2 July's bar alone, about 0.9788.
    assert mismatch.expected_close == Decimal("1557.09")
    assert report.rows == 2


def test_a_stretch_whose_every_close_is_zero_has_no_factor_and_no_close_mismatches(
    fresh_database_url: str,
) -> None:
    load(
        fresh_database_url,
        then(zero_prices(INFY_TOKEN, "2024-07-01"), zero_prices(INFY_TOKEN, "2024-07-02")),
    )

    report = run_short(fresh_database_url)

    assert report.close_mismatches == ()
    assert report.rows == 2
    assert report.differences == 0


# INFY's Kite prices as a tenth less 50 rupees: Kite's close is a x NSE's - b with
# a about 0.0979 and b 50, the recorded closes' own rounding shrunk below half a tick.
ADDITIVE = affine_prices(INFY_TOKEN, times="0.1", minus="50")


def test_a_stretch_whose_kite_closes_fit_a_times_nse_minus_b_is_an_additive_adjustment(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, ADDITIVE)

    report = run(fresh_database_url)

    # INFY's one stretch runs from 1 July to 30 August, 43 bars from about 1,590 to
    # 1,944: no one factor puts 105.71 for 1,590.8 and 140.25 for 1,943.7 within tolerance.
    assert len(report.close_mismatches) > 30
    assert {(d.symbol, d.reason) for d in report.close_mismatches} == {
        ("INFY", Reason.ADDITIVE_ADJUSTMENT)
    }
    assert report.unexplained == ()
    assert report.by_reason["additive_adjustment"] == len(report.close_mismatches)
    assert report.passed


# Monday 1 to Thursday 4 July 2024: one stretch of four INFY bars.
FOUR_START, FOUR_END = date(2024, 7, 1), date(2024, 7, 4)


def run_four(url: str, settings: Config.Parity = SETTINGS) -> ParityReport:
    with app_connection(url) as conn:
        return parity(conn, settings, start=FOUR_START, end=FOUR_END, symbols=["INFY"])


def test_the_same_shift_on_a_stretch_of_four_bars_stays_unexplained(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, ADDITIVE)

    report = run_four(fresh_database_url)

    assert report.rows == 4
    assert report.close_mismatches
    assert [d.reason for d in report.unexplained] == [None] * len(report.close_mismatches)
    assert not report.passed


def test_the_minimum_stretch_for_an_additive_adjustment_is_a_setting(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, ADDITIVE)

    report = run_four(fresh_database_url, Config.Parity(additive_min_bars=4))

    assert report.close_mismatches
    assert report.unexplained == ()


# INFY's Kite prices as a tenth, rounded to the rupee: a whole-rupee history of prices
# from about 156 to 194, where the 0.1 percent tolerance is under 20 paisa.
WHOLE_RUPEES = whole_rupees(INFY_TOKEN, times="0.1")


def test_a_close_equal_to_the_expected_close_rounded_to_the_rupee_is_coarse_rounding(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, WHOLE_RUPEES)

    report = run(fresh_database_url)

    assert len(report.close_mismatches) > 20
    assert {(d.symbol, d.reason) for d in report.close_mismatches} == {
        ("INFY", Reason.COARSE_ROUNDING)
    }
    assert all(d.candle_close == d.candle_close.to_integral() for d in report.close_mismatches)  # type: ignore[union-attr]
    assert report.unexplained == ()
    assert report.by_reason["coarse_rounding"] == len(report.close_mismatches)
    assert report.by_reason["additive_adjustment"] == 0
    assert report.passed


def test_a_close_fifty_paisa_off_the_expected_close_rounded_to_the_rupee_is_unexplained(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, then(WHOLE_RUPEES, shift_close(INFY_TOKEN, "2024-07-01", "0.5")))

    report = run(fresh_database_url)

    ((off),) = report.unexplained
    assert (off.kind, off.symbol, off.trade_date) == ("close", "INFY", date(2024, 7, 1))
    # 1,557.1 a tenth is 155.71, rounded to 156 and moved to 156.5.
    assert off.candle_close == Decimal("156.5")
    assert {d.reason for d in report.close_mismatches if d is not off} == {Reason.COARSE_ROUNDING}
    assert not report.passed


REPO_CONFIG = Path(__file__).resolve().parents[1] / "tradeline.toml"


def parity_command(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[dict[str, Any]], str]:
    monkeypatch.setenv("DATABASE_URL", url)
    code = main(["parity", "--from", START.isoformat(), "--to", END.isoformat()])
    captured = capsys.readouterr()
    return code, [json.loads(line) for line in captured.out.splitlines()], captured.err


def test_the_command_exits_zero_on_clean_fixtures_with_a_summary_and_a_table(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_CONFIG))
    load(fresh_database_url)

    code, lines, table = parity_command(fresh_database_url, monkeypatch, capsys)

    assert code == 0
    assert all(
        (line["range_start"], line["range_end"]) == ("2024-07-01", "2024-08-30") for line in lines
    )
    summary = lines[-1]
    assert summary["command"] == "parity"
    assert summary["msg"] == "parity finished"
    assert summary["report"] == {
        "start": "2024-07-01",
        "end": "2024-08-30",
        "close_tolerance": "0.001",
        "max_unexplained_pct": "0.1",
        "rows": ROWS,
        "differences": 0,
        "unexplained": 0,
        "unexplained_pct": "0.0000",
        "passed": True,
        "by_kind": {
            "missing": {"total": 0, "unexplained": 0},
            "extra": {"total": 0, "series_move": 0, "unexplained": 0},
            "close": {
                "total": 0,
                "additive_adjustment": 0,
                "coarse_rounding": 0,
                "unexplained": 0,
            },
        },
        "by_reason": {
            "series_move": 0,
            "additive_adjustment": 0,
            "coarse_rounding": 0,
            "unexplained": 0,
        },
        "listed_unexplained": [],
        "candle_disagreements": 0,
        "listed_candle_disagreements": [],
        "pre_listing_candles": 0,
        "listed_pre_listing_candles": [],
    }
    assert "172 rows, 0 differences" in table
    assert "PASS" in table


def test_the_command_exits_non_zero_above_the_configured_threshold(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))
    config = tmp_path / "tradeline.toml"

    config.write_text("[parity]\nmax_unexplained_pct = 0.1\n")
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))
    strict_code, strict_lines, strict_table = parity_command(
        fresh_database_url, monkeypatch, capsys
    )
    config.write_text("[parity]\nmax_unexplained_pct = 1.0\n")
    lenient_code, _, _ = parity_command(fresh_database_url, monkeypatch, capsys)

    assert strict_code == 1
    assert lenient_code == 0
    report = strict_lines[-1]["report"]
    assert report["passed"] is False
    assert report["listed_unexplained"] == [
        {
            "kind": "missing",
            "symbol": "INFY",
            "series": "EQ",
            "trade_date": "2024-07-15",
            "candle_close": None,
            "eod_close": "1707.05",
            "expected_close": None,
            "reason": None,
        }
    ]
    assert "1 unexplained (0.5814%), must be under 0.1%: FAIL" in strict_table
    assert (
        "by reason: series_move 0, additive_adjustment 0, coarse_rounding 0, unexplained 1"
        in strict_table
    )
    assert "missing  INFY" in strict_table


def test_every_line_of_the_command_carries_its_range_and_the_symbols_named(
    fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_CONFIG))
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    load(fresh_database_url)
    capsys.readouterr()

    code = main(["parity", "--from", "2024-07-01", "--to", "2024-07-31", "--symbol", "INFY"])

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert lines
    for line in lines:
        assert (line["range_start"], line["range_end"]) == ("2024-07-01", "2024-07-31")
        assert line["symbols"] == ["INFY"]
