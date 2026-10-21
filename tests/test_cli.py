import json
from pathlib import Path

import pytest

from tradeline.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


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
