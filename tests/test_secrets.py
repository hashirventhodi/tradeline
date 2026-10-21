from pathlib import Path

import pytest

from tradeline.environment import Environment
from tradeline.secrets import (
    DEFAULT_KITE_REDIRECT_URL,
    EnvSecrets,
    MissingSecret,
    SsmSecrets,
)


def write_env_file(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_development_reads_the_dotenv_file(tmp_path: Path) -> None:
    env_file = write_env_file(tmp_path / ".env", "KITE_API_KEY=abc123\n")

    secrets = EnvSecrets.for_environment(Environment.DEVELOPMENT, env_file=env_file, environ={})

    assert secrets.get("KITE_API_KEY") == "abc123"


def test_process_environment_wins_over_the_dotenv_file(tmp_path: Path) -> None:
    env_file = write_env_file(tmp_path / ".env", "KITE_API_KEY=from_file\n")

    secrets = EnvSecrets.for_environment(
        Environment.DEVELOPMENT, env_file=env_file, environ={"KITE_API_KEY": "from_process"}
    )

    assert secrets.get("KITE_API_KEY") == "from_process"


def test_dotenv_parsing_ignores_comments_blank_lines_and_quotes(tmp_path: Path) -> None:
    env_file = write_env_file(
        tmp_path / ".env",
        "# a comment\n\nKITE_API_KEY=\"quoted\"\nKITE_API_SECRET='single'\nexport DATABASE_URL=postgresql://x\n",
    )

    secrets = EnvSecrets.for_environment(Environment.DEVELOPMENT, env_file=env_file, environ={})

    assert secrets.get("KITE_API_KEY") == "quoted"
    assert secrets.get("KITE_API_SECRET") == "single"
    assert secrets.get("DATABASE_URL") == "postgresql://x"


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.TEST])
def test_outside_development_the_dotenv_file_is_not_read(
    tmp_path: Path, environment: Environment
) -> None:
    env_file = write_env_file(tmp_path / ".env", "KITE_API_KEY=from_file\n")

    secrets = EnvSecrets.for_environment(environment, env_file=env_file, environ={})

    assert secrets.get("KITE_API_KEY") is None


def test_require_raises_a_named_error_for_a_missing_secret() -> None:
    secrets = EnvSecrets.for_environment(Environment.PRODUCTION, environ={})

    with pytest.raises(MissingSecret, match="KITE_API_SECRET"):
        secrets.require("KITE_API_SECRET")


def test_get_falls_back_to_the_given_default() -> None:
    secrets = EnvSecrets.for_environment(Environment.PRODUCTION, environ={})

    assert secrets.get("KITE_REDIRECT_URL", DEFAULT_KITE_REDIRECT_URL) == (
        "http://127.0.0.1:8765/callback"
    )


def test_put_in_development_persists_to_the_dotenv_file_keeping_other_lines(
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path / ".env", "# keep me\nKITE_API_KEY=abc\nKITE_ACCESS_TOKEN=old\n"
    )
    secrets = EnvSecrets.for_environment(Environment.DEVELOPMENT, env_file=env_file, environ={})

    secrets.put("KITE_ACCESS_TOKEN", "new")
    secrets.put("KITE_ACCESS_TOKEN_ISSUED_ON", "2026-10-21")

    reloaded = EnvSecrets.for_environment(Environment.DEVELOPMENT, env_file=env_file, environ={})
    assert reloaded.get("KITE_ACCESS_TOKEN") == "new"
    assert reloaded.get("KITE_ACCESS_TOKEN_ISSUED_ON") == "2026-10-21"
    assert reloaded.get("KITE_API_KEY") == "abc"
    assert env_file.read_text().startswith("# keep me\n")


def test_put_in_production_is_visible_for_the_process_only(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    secrets = EnvSecrets.for_environment(Environment.PRODUCTION, env_file=env_file, environ={})

    secrets.put("KITE_ACCESS_TOKEN", "tok")

    assert secrets.get("KITE_ACCESS_TOKEN") == "tok"
    assert not env_file.exists()


def test_ssm_provider_is_a_stub_until_phase_6() -> None:
    secrets = SsmSecrets(parameter_path="/tradeline/")

    with pytest.raises(NotImplementedError, match="Phase 6"):
        secrets.get("KITE_API_KEY")
    with pytest.raises(NotImplementedError, match="Phase 6"):
        secrets.put("KITE_ACCESS_TOKEN", "tok")


def test_environment_is_read_from_tradeline_env_and_defaults_to_development() -> None:
    assert Environment.from_environ({}) is Environment.DEVELOPMENT
    assert Environment.from_environ({"TRADELINE_ENV": "test"}) is Environment.TEST
    assert Environment.from_environ({"TRADELINE_ENV": "production"}) is Environment.PRODUCTION


def test_unknown_tradeline_env_is_refused() -> None:
    with pytest.raises(ValueError, match="staging"):
        Environment.from_environ({"TRADELINE_ENV": "staging"})
