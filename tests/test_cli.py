import io
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradeline.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


def json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def repo_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_ROOT / "tradeline.toml"))


def test_migrate_applies_the_schema_then_is_a_no_op_logging_json_throughout(
    empty_database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", empty_database_url)

    assert main(["migrate"]) == 0
    first = json_lines(capsys.readouterr().out)
    assert main(["migrate"]) == 0
    second = json_lines(capsys.readouterr().out)

    assert first and all(line["command"] == "migrate" for line in first + second)
    assert any(line.get("migration") == "0001_ingest_runs" for line in first)
    assert not any("migration" in line for line in second)
    assert second[-1]["applied"] == 0


def test_migrate_without_a_database_url_fails_with_a_json_error_line(
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert main(["migrate"]) == 1

    (line,) = json_lines(capsys.readouterr().out)
    assert line["level"] == "ERROR"
    assert line["command"] == "migrate"
    assert "DATABASE_URL" in str(line["msg"])


def test_an_unknown_command_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["frobnicate"])

    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ingest_eod"


def test_ingest_eod_plays_fixtures_end_to_end_and_logs_every_run(
    database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    code = main(
        [
            "ingest",
            "eod",
            "--from",
            "2024-07-01",
            "--to",
            "2024-07-05",
            "--play-fixtures",
            str(FIXTURES),
        ]
    )

    assert code == 0
    lines = json_lines(capsys.readouterr().out)
    assert all(line["command"] == "ingest" for line in lines)
    finished = [line for line in lines if line["msg"] == "ingest run finished"]
    assert [line["source"] for line in finished] == [
        "nse_equity_master",
        "nse_eod",
        "kite_instruments",
    ]
    assert all(line["ingest_run_id"] for line in finished)
    assert (finished[1]["range_start"], finished[1]["range_end"]) == ("2024-07-01", "2024-07-05")
    assert finished[1]["rows_written"] == 25  # five days, five symbols
    assert lines[-1]["msg"] == "ingest eod finished"
    assert lines[-1]["days_loaded"] == 5


def test_ingest_eod_without_fixtures_under_test_refuses_the_network(
    database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    assert main(["ingest", "eod", "--from", "2024-07-01"]) == 1

    failure = json_lines(capsys.readouterr().out)[-1]
    assert failure["level"] == "ERROR"
    assert "refusing network call" in str(failure["exc_info"])


def test_ingest_eod_rejects_a_date_that_is_not_iso(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["ingest", "eod", "--from", "01-07-2024"])

    assert exit_info.value.code == 2
    assert "invalid" in capsys.readouterr().err


LOGIN_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "login"
CANDLE_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ingest_candles"


def test_login_prints_the_url_exchanges_the_pasted_token_and_stores_it(
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "stale")
    monkeypatch.setenv("KITE_ACCESS_TOKEN_ISSUED_ON", "2020-01-01")
    monkeypatch.setattr("sys.stdin", io.StringIO("test_request_token\n"))

    code = main(["login", "--play-fixtures", str(LOGIN_FIXTURES)])

    assert code == 0
    captured = capsys.readouterr()
    assert "https://kite.trade/connect/login?v=3&api_key=test_api_key" in captured.err
    lines = json_lines(captured.out)
    assert all(line["command"] == "login" for line in lines)
    assert "test_api_key" not in captured.out  # the API key is for the Operator's eyes only
    assert lines[-1]["user_id"] == "AB1234"
    assert os.environ["KITE_ACCESS_TOKEN"] == "test_access_token"
    assert os.environ["KITE_ACCESS_TOKEN_ISSUED_ON"] == datetime.now(IST).date().isoformat()


def test_login_takes_the_request_token_as_an_option_and_reports_a_rejected_one(
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "stale")

    code = main(
        ["login", "--request-token", "bad_request_token", "--play-fixtures", str(LOGIN_FIXTURES)]
    )

    assert code == 1
    failure = json_lines(capsys.readouterr().out)[-1]
    assert failure["level"] == "ERROR"
    assert "TokenException" in str(failure["msg"])
    assert os.environ["KITE_ACCESS_TOKEN"] == "stale"


def test_ingest_candles_plays_fixtures_end_to_end_and_logs_the_run(
    database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "test_access_token")
    assert main(["ingest", "eod", "--from", "2024-07-01", "--play-fixtures", str(FIXTURES)]) == 0
    capsys.readouterr()

    code = main(
        [
            "ingest",
            "candles",
            "--interval",
            "day",
            "--symbol",
            "INFY",
            "--from",
            "2024-07-01",
            "--to",
            "2024-08-30",
            "--play-fixtures",
            str(CANDLE_FIXTURES),
        ]
    )

    assert code == 0
    lines = json_lines(capsys.readouterr().out)
    assert all(line["command"] == "ingest" for line in lines)
    (finished,) = [line for line in lines if line["msg"] == "ingest run finished"]
    assert finished["source"] == "kite_candles_daily"
    assert (finished["range_start"], finished["range_end"]) == ("2024-07-01", "2024-08-30")
    assert finished["rows_written"] == 43
    assert lines[-1]["msg"] == "ingest candles finished"
    assert lines[-1]["symbols_loaded"] == 1


ACTION_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ingest_corporate_actions"


def test_ingest_corporate_actions_plays_fixtures_end_to_end_and_logs_the_run(
    database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    code = main(
        [
            "ingest",
            "corporate-actions",
            "--from",
            "2024-07-01",
            "--to",
            "2024-12-31",
            "--play-fixtures",
            str(ACTION_FIXTURES),
        ]
    )

    assert code == 0
    lines = json_lines(capsys.readouterr().out)
    assert all(line["command"] == "ingest" for line in lines)
    (finished,) = [line for line in lines if line["msg"] == "ingest run finished"]
    assert finished["source"] == "nse_corporate_actions"
    assert (finished["range_start"], finished["range_end"]) == ("2024-07-01", "2024-12-31")
    assert (finished["rows_seen"], finished["rows_written"]) == (20, 19)
    assert finished["by_kind"] == {"bonus": 2, "dividend": 8, "other": 7, "rights": 1, "split": 2}
    assert lines[-1]["msg"] == "ingest corporate-actions finished"
    assert lines[-1]["unparsed"] == 0


def test_ingest_candles_without_an_access_token_fails_by_name(
    database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)

    code = main(["ingest", "candles", "--interval", "day", "--from", "2024-07-01"])

    assert code == 1
    failure = json_lines(capsys.readouterr().out)[-1]
    assert "KITE_ACCESS_TOKEN" in str(failure["msg"])


def test_ingest_candles_accepts_only_the_day_interval_for_now(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["ingest", "candles", "--interval", "minute", "--from", "2024-07-01"])

    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


UNIVERSE_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "universe"


def test_universe_computes_each_month_under_the_configured_rule_and_logs_it(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "tradeline.toml"
    config.write_text(
        "[universe]\ntop_n = 3\nlookback_trading_days = 20\nmin_listing_trading_days = 21\n"
    )
    monkeypatch.setenv("TRADELINE_CONFIG", str(config))
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert (
        main(
            [
                "ingest",
                "eod",
                "--from",
                "2024-07-01",
                "--to",
                "2024-08-02",
                "--play-fixtures",
                str(UNIVERSE_FIXTURES),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["universe", "--month", "2024-08"]) == 0

    lines = json_lines(capsys.readouterr().out)
    assert all(line["command"] == "universe" for line in lines)
    computed = [line for line in lines if line["msg"] == "universe computed"]
    assert [(line["month"], line["members"]) for line in computed] == [("2024-08-01", 3)]
    assert len(str(computed[0]["rule_version"])) == 64


def test_universe_reports_a_month_it_cannot_compute_as_a_json_error(
    database_url: str,
    repo_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    assert main(["universe", "--month", "2010-01"]) == 1

    failure = json_lines(capsys.readouterr().out)[-1]
    assert failure["level"] == "ERROR"
    assert "trading days of end-of-day files" in str(failure["msg"])
