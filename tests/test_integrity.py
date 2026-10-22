"""``integrity``: NSE's data as we hold it, checked against NSE alone (ADR 0018).

The end-of-day fixtures (``ingest_eod``) hold every file NSE published from
2024-07-01 to 2024-08-30: each weekday but two holidays, 2024-07-17
(Muharram) and 2024-08-15 (Independence Day), and no weekend. Each test
writes a small Trading Calendar into a temporary directory. A lost or extra
file is made by what is ingested: a day left out of the ingested range, or a
weekday's recorded file played back as a weekend's (``playback``), so no
fixture file is edited.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from playback import Playback, bhavcopy_as_of
from tradeline import queries
from tradeline.cli import main
from tradeline.config import Config
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.integrity import DayFailure, IntegrityReport, Span, integrity
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures"
START, END = date(2024, 7, 1), date(2024, 8, 30)
HEADER = "date,kind,citation\n"
# NSE's holiday list for 2024 (the citation is the test's, not a reviewed one).
HOLIDAYS = "2024-07-17,holiday,NSE/CMTR/59722\n2024-08-15,holiday,NSE/CMTR/59722\n"


def calendar(tmp_path: Path, rows: str, *, covers: str = "2024-01-01,2024-12-31") -> Path:
    path = tmp_path / "trading_calendar.csv"
    path.write_text(f"covers,{covers}\n" + HEADER + rows)
    return path


@pytest.fixture(scope="module")
def loaded(database_url: str) -> str:
    """Every recorded end-of-day file; tests here only read it."""
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(FIXTURES / "ingest_eod"), start=START, end=END)
    return database_url


def no_large_move_records(tmp_path: Path) -> Path:
    path = tmp_path / "large_moves.csv"
    path.write_text("symbol,trade_date,reason,citation\n")
    return path


def run(url: str, path: Path, start: date = START, end: date = END) -> IntegrityReport:
    settings = Config.Integrity(
        trading_calendar=str(path), large_moves=str(no_large_move_records(path.parent))
    )
    with app_connection(url) as conn:
        return integrity(conn, settings, start=start, end=end)


def test_a_calendar_listing_both_holidays_passes_over_the_recorded_files(
    loaded: str, tmp_path: Path
) -> None:
    report = run(loaded, calendar(tmp_path, HOLIDAYS))

    assert report.passed
    assert report.day_failures == ()
    assert report.uncovered == ()


def test_a_weekday_missing_its_file_fails_naming_the_day(
    fresh_database_url: str, tmp_path: Path
) -> None:
    lost = date(2024, 7, 10)
    with app_connection(fresh_database_url) as conn:
        eod = PlaybackTransport(FIXTURES / "ingest_eod")
        ingest_eod(conn, eod, start=START, end=lost - timedelta(days=1))
        ingest_eod(conn, eod, start=lost + timedelta(days=1), end=END)

    report = run(fresh_database_url, calendar(tmp_path, HOLIDAYS))

    assert not report.passed
    assert report.day_failures == (DayFailure("missing", lost),)
    assert "missing                  2024-07-10 Wed" in report.text()
    assert report.summary()["listed_day_failures"] == [
        {"kind": "missing", "trade_date": "2024-07-10", "citation": None}
    ]


def test_a_listed_holiday_with_a_file_fails_citing_the_row(loaded: str, tmp_path: Path) -> None:
    report = run(loaded, calendar(tmp_path, HOLIDAYS + "2024-08-14,holiday,NSE/CMTR/99999\n"))

    assert not report.passed
    assert report.day_failures == (
        DayFailure("holiday_with_file", date(2024, 8, 14), "NSE/CMTR/99999"),
    )


def test_a_listed_special_session_without_a_file_fails(loaded: str, tmp_path: Path) -> None:
    rows = HOLIDAYS + "2024-08-03,special_session,NSE/CMTR/88888\n"

    report = run(loaded, calendar(tmp_path, rows))

    assert not report.passed
    assert report.day_failures == (
        DayFailure("special_session_missing", date(2024, 8, 3), "NSE/CMTR/88888"),
    )
    assert report.summary()["special_session_missing"] == 1


def test_a_weekend_file_fails_unless_the_calendar_lists_it_as_a_special_session(
    fresh_database_url: str, tmp_path: Path
) -> None:
    saturday, friday = date(2024, 7, 13), date(2024, 7, 12)
    ask, edit = bhavcopy_as_of(saturday, recorded=friday)
    with app_connection(fresh_database_url) as conn:
        ingest_eod(conn, PlaybackTransport(FIXTURES / "ingest_eod"), start=START, end=END)
        moved = Playback(FIXTURES / "ingest_eod", ask=ask, edit=edit)
        ingest_eod(conn, moved, start=saturday, end=saturday)

    unlisted = run(fresh_database_url, calendar(tmp_path, HOLIDAYS))
    listed = run(
        fresh_database_url,
        calendar(tmp_path, HOLIDAYS + "2024-07-13,special_session,NSE/CMTR/77777\n"),
    )

    assert unlisted.day_failures == (DayFailure("unlisted_weekend_file", saturday),)
    assert not unlisted.passed
    assert listed.passed


def test_a_range_beyond_the_calendar_fails_and_its_covered_part_is_still_checked(
    loaded: str, tmp_path: Path
) -> None:
    path = calendar(tmp_path, HOLIDAYS, covers="2024-07-05,2024-08-20")

    report = run(loaded, path)

    assert not report.passed
    assert report.uncovered == (Span(START, date(2024, 7, 4)), Span(date(2024, 8, 21), END))
    assert report.day_failures == ()
    assert report.summary()["uncovered"] == [
        {"start": "2024-07-01", "end": "2024-07-04"},
        {"start": "2024-08-21", "end": "2024-08-30"},
    ]


def test_a_range_wholly_outside_the_calendar_fails_with_nothing_checked(
    loaded: str, tmp_path: Path
) -> None:
    report = run(loaded, calendar(tmp_path, "", covers="2025-01-01,2025-12-31"))

    assert not report.passed
    assert report.uncovered == (Span(START, END),)
    assert report.trading_days == 0


def integrity_command(
    url: str,
    rows: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, list[dict[str, Any]], str]:
    config = tmp_path / "tradeline.toml"
    config.write_text(
        f'[integrity]\ntrading_calendar = "{calendar(tmp_path, rows)}"\n'
        f'large_moves = "{no_large_move_records(tmp_path)}"\n'
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))
    monkeypatch.setenv("DATABASE_URL", url)
    capsys.readouterr()
    code = main(["integrity", "--from", START.isoformat(), "--to", END.isoformat()])
    captured = capsys.readouterr()
    return code, [json.loads(line) for line in captured.out.splitlines()], captured.err


def test_the_command_exits_zero_when_the_check_passes_with_a_summary_and_a_table(
    loaded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, table = integrity_command(loaded, HOLIDAYS, tmp_path, monkeypatch, capsys)

    assert code == 0
    assert all(
        (line["range_start"], line["range_end"]) == ("2024-07-01", "2024-08-30") for line in lines
    )
    summary = lines[-1]
    assert (summary["command"], summary["msg"]) == ("integrity", "integrity finished")
    assert summary["report"] == {
        "start": "2024-07-01",
        "end": "2024-08-30",
        "calendar_first": "2024-01-01",
        "calendar_last": "2024-12-31",
        "trading_days": 43,
        "uncovered": [],
        "day_failures": 0,
        "missing": 0,
        "special_session_missing": 0,
        "holiday_with_file": 0,
        "unlisted_weekend_file": 0,
        "listed_day_failures": [],
        "ever_members": 0,
        "unresolved_members": [],
        "large_moves": 0,
        "large_moves_explained": {},
        "unexplained_large_moves": 0,
        "listed_unexplained_large_moves": [],
        "unresolved_records": [],
        "unmatched_records": [],
        "gaps": 0,
        "gaps_by_instrument": {},
        "listed_gaps": [],
        "passed": True,
    }
    assert "43 trading days checked, 0 day failures" in table
    assert "PASS" in table


def test_the_command_exits_one_when_the_check_fails_and_writes_nothing(
    loaded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with app_connection(loaded) as conn:
        runs = queries.ingest_runs(conn)

    code, lines, table = integrity_command(
        loaded, "2024-08-15,holiday,NSE/CMTR/59722\n", tmp_path, monkeypatch, capsys
    )

    assert code == 1
    assert lines[-1]["level"] == "ERROR"
    assert lines[-1]["report"]["listed_day_failures"] == [
        {"kind": "missing", "trade_date": "2024-07-17", "citation": None}
    ]
    assert "FAIL" in table
    with app_connection(loaded) as conn:
        assert queries.ingest_runs(conn) == runs


def test_the_command_refuses_a_malformed_calendar_naming_the_line(
    loaded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = integrity_command(
        loaded, HOLIDAYS + "2024-08-16,holiday\n", tmp_path, monkeypatch, capsys
    )

    assert code == 1
    assert "trading_calendar.csv line 5" in lines[-1]["msg"]
