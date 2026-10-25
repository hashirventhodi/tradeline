"""The weekly Integrity Check over the data collected after a Protocol's freeze (ADR 0007,
2026-10-25 amendment).

A week is played back from the recorded end-of-day files and Kite candles
(``ingest_eod``, ``ingest_candles``) as ``collect-daily`` would have loaded it after a
freeze on Friday 2024-07-05: Monday 2024-07-08 to Friday 2024-07-12. A lost file is a day
left out of the ingest, and a lost candle a bar dropped on its way to it (``playback``), so
no fixture is edited.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from playback import (
    Candle,
    CandleEdit,
    Playback,
    drop_bar,
    in_requested_range,
    kite_candles,
    recorded_range,
)
from synthetic_eod import base_market, freeze_protocol, load_market, repo_calendar, write_fixtures
from tradeline.cli import main
from tradeline.config import Config
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.eod import ingest_eod
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport, Request
from tradeline.weekly_integrity import WeeklyIntegrityReport, weekly_integrity

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED_START, RECORDED_END = date(2024, 7, 1), date(2024, 8, 30)
FROZEN_ON = date(2024, 7, 5)
MONDAY, FRIDAY = date(2024, 7, 8), date(2024, 7, 12)
INFY_TOKEN = "408065"


def untouched(request: Request, candles: list[Candle]) -> list[Candle]:
    return candles


def calendar(tmp_path: Path) -> Path:
    path = tmp_path / "trading_calendar.csv"
    path.write_text(
        "covers,2024-01-01,2024-12-31\ndate,kind,citation\n2024-07-17,holiday,NSE/CMTR/59722\n"
    )
    return path


def settings(tmp_path: Path) -> tuple[Config.Integrity, Config.Parity]:
    path = calendar(tmp_path)
    large_moves = tmp_path / "large_moves.csv"
    large_moves.write_text("symbol,trade_date,reason,citation\n")
    return (
        Config.Integrity(trading_calendar=str(path), large_moves=str(large_moves)),
        Config.Parity(trading_calendar=str(path)),
    )


def collect_week(url: str, *, lost: date | None = None, edit: CandleEdit = untouched) -> None:
    """The week's end-of-day files but ``lost``, and Kite's candles for every day of it."""
    eod = PlaybackTransport(FIXTURES / "ingest_eod")
    with app_connection(url) as conn:
        days = [MONDAY + timedelta(days=n) for n in range(5)]
        for day in days:
            if day != lost:
                ingest_eod(conn, eod, start=day, end=day)
        transport = Playback(
            FIXTURES / "ingest_candles",
            ask=recorded_range(RECORDED_START.isoformat(), RECORDED_END.isoformat()),
            edit=kite_candles(lambda r, c: edit(r, in_requested_range(r, c))),
        )
        session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
        ingest_candles(conn, session, start=MONDAY, end=FRIDAY)


def check(url: str, tmp_path: Path) -> WeeklyIntegrityReport:
    integrity, parity = settings(tmp_path)
    with app_connection(url) as conn:
        return weekly_integrity(conn, integrity, parity, after=FROZEN_ON, end=FRIDAY)


def test_a_complete_week_after_the_freeze_raises_no_alert(
    fresh_database_url: str, tmp_path: Path
) -> None:
    collect_week(fresh_database_url)

    report = check(fresh_database_url, tmp_path)

    assert report.alerts == ()
    assert report.passed
    # Every day after the freeze day is checked, the weekend's too.
    assert (report.start, report.end) == (date(2024, 7, 6), FRIDAY)
    assert report.integrity is not None
    assert report.integrity.trading_days == 5
    assert report.parity_gaps == ()


def test_a_week_missing_an_end_of_day_file_alerts_naming_the_day(
    fresh_database_url: str, tmp_path: Path
) -> None:
    collect_week(fresh_database_url, lost=date(2024, 7, 10))

    report = check(fresh_database_url, tmp_path)

    assert not report.passed
    assert report.alerts == ("no end-of-day file for 2024-07-10 Wed, a trading day",)


def test_a_missing_kite_candle_is_a_parity_gap_and_never_an_integrity_failure(
    fresh_database_url: str, tmp_path: Path
) -> None:
    collect_week(fresh_database_url, edit=drop_bar(INFY_TOKEN, "2024-07-10"))

    report = check(fresh_database_url, tmp_path)

    assert report.passed
    assert report.alerts == ()
    assert [(g.symbol, g.series, g.trade_date, g.reason) for g in report.parity_gaps] == [
        ("INFY", "EQ", date(2024, 7, 10), None)
    ]
    gaps = report.summary()["kite_parity_gaps"]
    assert [(g["symbol"], g["trade_date"], g["reason"]) for g in gaps] == [
        ("INFY", "2024-07-10", None)
    ]
    assert "Kite Parity gap 2024-07-10 INFY EQ (never an Integrity failure)" in report.text()


def weekly_command(
    url: str,
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, list[dict[str, Any]], str]:
    integrity, _ = settings(tmp_path)
    config = tmp_path / "tradeline.toml"
    config.write_text(
        f'[parity]\ntrading_calendar = "{integrity.trading_calendar}"\n'
        f'[integrity]\ntrading_calendar = "{integrity.trading_calendar}"\n'
        f'large_moves = "{integrity.large_moves}"\n'
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))
    monkeypatch.setenv("DATABASE_URL", url)
    capsys.readouterr()
    code = main(["integrity-weekly", *arguments])
    captured = capsys.readouterr()
    return code, [json.loads(line) for line in captured.out.splitlines()], captured.err


def test_the_command_logs_the_alert_at_error_and_exits_one(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collect_week(fresh_database_url, lost=date(2024, 7, 10))

    code, lines, table = weekly_command(
        fresh_database_url,
        ["--after", FROZEN_ON.isoformat(), "--to", FRIDAY.isoformat()],
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert code == 1
    alert = lines[-1]
    assert (alert["level"], alert["msg"]) == ("ERROR", "integrity alert")
    assert alert["report"]["alerts"] == ["no end-of-day file for 2024-07-10 Wed, a trading day"]
    assert "ALERT no end-of-day file for 2024-07-10 Wed" in table


def test_the_command_passes_quietly_on_a_complete_week(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collect_week(fresh_database_url)

    code, lines, _ = weekly_command(
        fresh_database_url,
        ["--after", FROZEN_ON.isoformat(), "--to", FRIDAY.isoformat()],
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert code == 0
    assert (lines[-1]["level"], lines[-1]["msg"]) == ("INFO", "weekly integrity finished")
    assert lines[-1]["report"]["alerts"] == []


def test_a_protocol_frozen_today_has_nothing_after_its_freeze_to_check_yet(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    market = base_market()
    fixtures = write_fixtures(market, tmp_path / "synthetic", repo_calendar())
    with app_connection(fresh_database_url) as conn:
        load_market(conn, fixtures, market)
        frozen = freeze_protocol(conn, market, date(2023, 1, 2), date(2024, 6, 28))
    today = datetime.now(IST).date()

    code, lines, table = weekly_command(
        fresh_database_url, ["--protocol", frozen.hash[:12]], tmp_path, monkeypatch, capsys
    )

    assert code == 0
    report = lines[-1]["report"]
    assert (report["start"], report["end"]) == (
        (today + timedelta(days=1)).isoformat(),
        today.isoformat(),
    )
    assert report["alerts"] == []
    assert report["integrity"] is None
    assert "nothing after the freeze to check yet" in table


def test_the_command_refuses_a_protocol_never_frozen(
    fresh_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, lines, _ = weekly_command(
        fresh_database_url, ["--protocol", "0" * 12], tmp_path, monkeypatch, capsys
    )

    assert code == 1
    assert lines[-1]["msg"] == "no Protocol 000000000000 is frozen"
