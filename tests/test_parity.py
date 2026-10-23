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
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
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
    in_requested_range,
    kite_candles,
    recorded_range,
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
# No Special Session in range; a test of one writes its own calendar (``calendar``).
CALENDAR = FIXTURES / "trading_calendar.csv"
SETTINGS = Config.Parity(
    close_tolerance=0.001, max_unexplained_pct=0.1, trading_calendar=str(CALENDAR)
)
HUNDRED = Decimal(100)

INFY_TOKEN = "408065"


def untouched(request: Request, candles: list[Candle]) -> list[Candle]:
    return candles


def load(
    url: str, edit: CandleEdit = untouched, eod: Edit | None = None, *, candles_end: date = END
) -> None:
    """The fixtures loaded, Kite asked for candles from START to ``candles_end``."""
    with app_connection(url) as conn:
        ingest_eod(conn, Playback(FIXTURES / "ingest_eod", edit=eod), start=START, end=END)
        ingest_corporate_actions(
            conn,
            PlaybackTransport(FIXTURES / "ingest_corporate_actions"),
            start=ACTIONS_START,
            end=ACTIONS_END,
        )
        transport = Playback(
            FIXTURES / "ingest_candles",
            ask=recorded_range(START.isoformat(), END.isoformat()),
            edit=kite_candles(then(in_requested_range, edit)),
        )
        session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
        ingest_candles(conn, session, start=START, end=candles_end)


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

    strict = run(fresh_database_url, replace(SETTINGS, max_unexplained_pct=0.1))
    lenient = run(fresh_database_url, replace(SETTINGS, max_unexplained_pct=1.0))

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
            replace(SETTINGS, max_unexplained_pct=6.25),
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
        "missing": {"total": 1, "special_session": 0, "not_fetched": 0, "unexplained": 1},
        "extra": {"total": 0, "series_move": 0, "unexplained": 0},
        "close": {
            "total": 1,
            "kites_adjustment": 0,
            "unexplained": 1,
        },
    }
    assert summary["by_reason"] == {
        "series_move": 0,
        "special_session": 0,
        "not_fetched": 0,
        "kites_adjustment": 0,
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
    assert report.by_reason == {
        "series_move": 0,
        "special_session": 0,
        "not_fetched": 0,
        "kites_adjustment": 0,
        "unexplained": 1,
    }
    assert not report.passed


def calendar(tmp_path: Path, rows: str) -> Config.Parity:
    """The settings with a Trading Calendar of ``rows`` (the citations are the tests')."""
    path = tmp_path / "trading_calendar.csv"
    path.write_text("covers,2024-01-01,2024-12-31\ndate,kind,citation\n" + rows)
    return replace(SETTINGS, trading_calendar=str(path))


def test_a_bar_dropped_on_a_special_session_is_explained_and_the_verdict_ignores_it(
    fresh_database_url: str, tmp_path: Path
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    report = run(
        fresh_database_url, calendar(tmp_path, "2024-07-15,special_session,NSE/CMTR/77777\n")
    )

    ((missing),) = report.missing
    assert (missing.symbol, missing.trade_date) == ("INFY", date(2024, 7, 15))
    assert missing.reason == Reason.SPECIAL_SESSION
    assert report.unexplained == ()
    assert report.passed
    summary = report.summary()
    assert summary["by_kind"]["missing"] == {
        "total": 1,
        "special_session": 1,
        "not_fetched": 0,
        "unexplained": 0,
    }
    assert summary["by_reason"] == {
        "series_move": 0,
        "special_session": 1,
        "not_fetched": 0,
        "kites_adjustment": 0,
        "unexplained": 0,
    }
    assert summary["listed_unexplained"] == []
    assert (
        "by reason: series_move 0, special_session 1, not_fetched 0, kites_adjustment 0,"
        " unexplained 0"
    ) in report.text()


def test_the_same_bar_dropped_on_an_ordinary_day_stays_unexplained(
    fresh_database_url: str, tmp_path: Path
) -> None:
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"))

    report = run(
        fresh_database_url, calendar(tmp_path, "2024-07-16,special_session,NSE/CMTR/77777\n")
    )

    ((missing),) = report.missing
    assert missing.trade_date == date(2024, 7, 15)
    assert missing.reason is None
    assert report.unexplained == (missing,)
    assert not report.passed


def test_a_malformed_calendar_refuses_the_run_naming_the_line(
    fresh_database_url: str, tmp_path: Path
) -> None:
    load(fresh_database_url)

    with pytest.raises(ValueError, match="trading_calendar.csv line 3"):
        run(fresh_database_url, calendar(tmp_path, "2024-07-15,special_session\n"))


def test_nse_rows_outside_every_requested_range_are_not_fetched_while_a_dropped_bar_is_not(
    fresh_database_url: str,
) -> None:
    """Kite is asked for July alone, and INFY's 15 July bar is dropped inside it."""
    load(fresh_database_url, drop_bar(INFY_TOKEN, "2024-07-15"), candles_end=date(2024, 7, 31))

    report = run(fresh_database_url)

    not_fetched = [d for d in report.missing if d.reason == Reason.NOT_FETCHED]
    # August 2024 less the 15th's holiday: 21 days of each EQ symbol.
    assert len(not_fetched) == 21 * EQ_SYMBOLS
    assert {d.trade_date.month for d in not_fetched} == {8}
    assert [(d.kind, d.symbol, d.trade_date) for d in report.unexplained] == [
        ("missing", "INFY", date(2024, 7, 15))
    ]
    assert report.rows == ROWS
    # One unexplained row in 172: the not-fetched ones do not count against parity.
    assert report.unexplained_pct == HUNDRED / ROWS
    summary = report.summary()
    assert summary["by_kind"]["missing"] == {
        "total": 21 * EQ_SYMBOLS + 1,
        "special_session": 0,
        "not_fetched": 21 * EQ_SYMBOLS,
        "unexplained": 1,
    }
    assert summary["by_reason"] == {
        "series_move": 0,
        "special_session": 0,
        "not_fetched": 21 * EQ_SYMBOLS,
        "kites_adjustment": 0,
        "unexplained": 1,
    }
    assert (
        "by reason: series_move 0, special_session 0, not_fetched 84, kites_adjustment 0,"
        " unexplained 1" in report.text()
    )
    # 85 missing of 172 rows would fail any threshold; the one unexplained passes 1 percent.
    assert run(fresh_database_url, replace(SETTINGS, max_unexplained_pct=1.0)).passed


def test_a_special_session_outside_every_requested_range_is_a_special_session(
    fresh_database_url: str, tmp_path: Path
) -> None:
    """ADR 0018 tries a Special Session before not fetched."""
    load(fresh_database_url, candles_end=date(2024, 7, 31))

    report = run(
        fresh_database_url, calendar(tmp_path, "2024-08-01,special_session,NSE/CMTR/77777\n")
    )

    reasons = {(d.trade_date, d.reason) for d in report.missing}
    assert (date(2024, 8, 1), Reason.SPECIAL_SESSION) in reasons
    assert (date(2024, 8, 1), Reason.NOT_FETCHED) not in reasons
    assert report.by_reason == {
        "series_move": 0,
        "special_session": EQ_SYMBOLS,
        "not_fetched": 20 * EQ_SYMBOLS,
        "kites_adjustment": 0,
        "unexplained": 0,
    }


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
# a about 0.0979 and b 50, which was #17's additive adjustment. No action of INFY's
# subtracts 50 or divides by ten, so no stage of Kite's Adjustment reproduces it.
ADDITIVE = affine_prices(INFY_TOKEN, times="0.1", minus="50")


def test_a_stretch_whose_kite_closes_fit_a_line_no_stage_gives_stays_unexplained(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, ADDITIVE)

    report = run(fresh_database_url)

    # INFY's one stretch runs from 1 July to 30 August, 43 bars from about 1,590 to
    # 1,944: no one factor puts 105.71 for 1,590.8 and 140.25 for 1,943.7 within tolerance.
    assert len(report.close_mismatches) > 30
    assert {(d.symbol, d.reason) for d in report.close_mismatches} == {("INFY", None)}
    assert report.kite_bases == ()
    assert not report.passed


# INFY's Kite prices as a tenth, rounded to the rupee: #17's coarse rounding. Its one
# action, the dividend of 29 October 2024, can only divide or round again, not by ten.
WHOLE_RUPEES = whole_rupees(INFY_TOKEN, times="0.1")


def test_a_close_rounded_to_the_rupee_that_no_stage_reproduces_stays_unexplained(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, WHOLE_RUPEES)

    report = run(fresh_database_url)

    assert len(report.close_mismatches) > 20
    assert {d.reason for d in report.close_mismatches} == {None}
    assert not report.passed


RELIANCE_TOKEN = "738561"


def kite_from_nse(
    url: str, token: str, symbol: str, kite: Callable[[Decimal], Decimal]
) -> CandleEdit:
    """Instrument ``token``'s every close set to ``kite`` of NSE's close that day, read
    from the end-of-day rows ``load`` has ingested by the time Kite is asked."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        with app_connection(url) as conn:
            nse: dict[date, Decimal] = dict(
                conn.execute(
                    "SELECT trade_date, close FROM eod_bhavcopy"
                    " WHERE symbol = %s AND series = 'EQ'",
                    (symbol,),
                ).fetchall()
            )
        for candle in candles:
            candle[4] = float(kite(nse[date.fromisoformat(candle[0][:10])]))
            candle[2] = max(candle[2], candle[4])
            candle[3] = min(candle[3], candle[4])
        return candles

    return edit


def halved_to_five_rupees(nse: Decimal) -> Decimal:
    """RELIANCE's 1:1 bonus of 28 October 2024, rounded to Kite's ₹5 tick, half to even."""
    return (nse / 2 / 5).quantize(Decimal(1), ROUND_HALF_EVEN) * 5


def test_closes_kites_adjustment_reproduces_are_explained_and_its_kite_basis_reported(
    fresh_database_url: str,
) -> None:
    url = fresh_database_url
    load(url, kite_from_nse(url, RELIANCE_TOKEN, "RELIANCE", halved_to_five_rupees))

    report = run(url)

    # Up to ₹2.50 off on about 1,500, against a tolerance of about ₹1.53.
    assert len(report.close_mismatches) > 5
    assert {(d.symbol, d.reason) for d in report.close_mismatches} == {
        ("RELIANCE", Reason.KITES_ADJUSTMENT)
    }
    assert report.passed
    # Both stretches, either side of the Rs 10 dividend of 19 August, in one basis:
    # the dividend, before 1 October 2024 and under 2 percent, is no stage.
    ((replayed),) = report.kite_bases
    assert (replayed.instrument, replayed.series) == ("RELIANCE", "EQ")
    assert (replayed.basis.start, replayed.basis.end, replayed.basis.bars) == (START, END, 43)
    ((stage),) = replayed.basis.stages
    assert (stage.ex_date, stage.form, stage.divisor, stage.tick) == (
        date(2024, 10, 28), "split", Fraction(2), Fraction(5),
    )  # fmt: skip
    assert (replayed.explained, replayed.unexplained) == (len(report.close_mismatches), 0)
    assert report.summary()["listed_kite_bases"] == [
        {
            "instrument": "RELIANCE",
            "series": "EQ",
            "start": "2024-07-01",
            "end": "2024-08-30",
            "bars": 43,
            "explains": True,
            "stages": [
                {
                    "ex_date": "2024-10-28",
                    "how": "computed",
                    "form": "split",
                    "divisor": "2",
                    "subtracted": "0",
                    "tick": "5",
                    "ties": "half_even",
                }
            ],
            "explained": len(report.close_mismatches),
            "unexplained": 0,
            "listed_unexplained_days": [],
        }
    ]
    assert report.summary()["kites_adjustment_through_fitted"] == 0
    assert (
        "RELIANCE EQ 2024-07-01 to 2024-08-30, 43 bars: 2024-10-28 split divides by 2,"
        f" rounds to 5 half_even; {len(report.close_mismatches)} explained, 0 unexplained"
    ) in report.text()


def test_a_close_off_the_replay_stays_unexplained_while_the_rest_are_explained(
    fresh_database_url: str,
) -> None:
    url = fresh_database_url
    load(
        url,
        then(
            kite_from_nse(url, RELIANCE_TOKEN, "RELIANCE", halved_to_five_rupees),
            shift_close(RELIANCE_TOKEN, "2024-07-01", "5"),
        ),
    )

    report = run(url)

    ((off),) = report.unexplained
    assert (off.symbol, off.trade_date) == ("RELIANCE", date(2024, 7, 1))
    assert {d.reason for d in report.close_mismatches if d is not off} == {Reason.KITES_ADJUSTMENT}
    ((replayed),) = report.kite_bases
    assert replayed.unexplained_days == (date(2024, 7, 1),)


def test_a_kite_basis_of_fewer_than_twenty_bars_explains_none_of_its_closes(
    fresh_database_url: str,
) -> None:
    url = fresh_database_url
    load(url, kite_from_nse(url, RELIANCE_TOKEN, "RELIANCE", halved_to_five_rupees))

    # 1 to 26 July 2024: 19 trading days, 17 July a holiday.
    with app_connection(url) as conn:
        report = parity(conn, SETTINGS, start=START, end=date(2024, 7, 26), symbols=["RELIANCE"])

    assert report.rows == 19
    assert report.close_mismatches
    assert {d.reason for d in report.close_mismatches} == {None}
    ((replayed),) = report.kite_bases
    assert (replayed.basis.bars, replayed.basis.explains) == (19, False)


REPO_CONFIG = Path(__file__).resolve().parents[1] / "tradeline.toml"


def in_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from a directory holding the test calendar at the committed config's path."""
    shutil.copy(CALENDAR, tmp_path / "trading_calendar.csv")
    monkeypatch.chdir(tmp_path)


def parity_command(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[dict[str, Any]], str]:
    monkeypatch.setenv("DATABASE_URL", url)
    code = main(["parity", "--from", START.isoformat(), "--to", END.isoformat()])
    captured = capsys.readouterr()
    return code, [json.loads(line) for line in captured.out.splitlines()], captured.err


def test_the_command_exits_zero_on_clean_fixtures_with_a_summary_and_a_table(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_CONFIG))
    in_a_checkout(tmp_path, monkeypatch)
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
            "missing": {"total": 0, "special_session": 0, "not_fetched": 0, "unexplained": 0},
            "extra": {"total": 0, "series_move": 0, "unexplained": 0},
            "close": {
                "total": 0,
                "kites_adjustment": 0,
                "unexplained": 0,
            },
        },
        "by_reason": {
            "series_move": 0,
            "special_session": 0,
            "not_fetched": 0,
            "kites_adjustment": 0,
            "unexplained": 0,
        },
        "listed_unexplained": [],
        "kite_bases": 0,
        "kites_adjustment_through_fitted": 0,
        "listed_kite_bases": [],
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

    config.write_text(
        f'[parity]\nmax_unexplained_pct = 0.1\ntrading_calendar = "{CALENDAR}"\n'
        f'[integrity]\ntrading_calendar = "{CALENDAR}"\n'
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))
    strict_code, strict_lines, strict_table = parity_command(
        fresh_database_url, monkeypatch, capsys
    )
    config.write_text(
        f'[parity]\nmax_unexplained_pct = 1.0\ntrading_calendar = "{CALENDAR}"\n'
        f'[integrity]\ntrading_calendar = "{CALENDAR}"\n'
    )
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
        "by reason: series_move 0, special_session 0, not_fetched 0, kites_adjustment 0,"
        " unexplained 1"
    ) in strict_table
    assert "missing  INFY" in strict_table


def test_every_line_of_the_command_carries_its_range_and_the_symbols_named(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_CONFIG))
    in_a_checkout(tmp_path, monkeypatch)
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


def test_the_command_refuses_a_malformed_calendar_naming_the_line(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load(fresh_database_url)
    settings = calendar(tmp_path, "2024-07-15,special_session\n")
    config = tmp_path / "tradeline.toml"
    config.write_text(
        f'[parity]\ntrading_calendar = "{settings.trading_calendar}"\n'
        f'[integrity]\ntrading_calendar = "{settings.trading_calendar}"\n'
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))

    code, lines, _ = parity_command(fresh_database_url, monkeypatch, capsys)

    assert code == 1
    assert lines[-1]["level"] == "ERROR"
    assert "trading_calendar.csv line 3" in lines[-1]["msg"]
