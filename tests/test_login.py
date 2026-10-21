"""``login``: a pasted request token becomes the day's stored access token.

The token-exchange fixtures are written by hand in Kite's documented response
shape with placeholder credentials, because a real recording would carry the
Operator's API key in the request body and their account details in the
response. Everything else about the exchange (URL, form body, checksum) is
exercised for real: a fixture is served only when the adapter's request
matches it byte for byte.
"""

from datetime import date
from pathlib import Path

import pytest

from tradeline.environment import Environment
from tradeline.login import login, login_url_for
from tradeline.secrets import EnvSecrets, MissingSecret
from tradeline.sources.kite import KiteError
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures" / "login"
TODAY = date(2026, 10, 21)


@pytest.fixture
def secrets(tmp_path: Path) -> EnvSecrets:
    env_file = tmp_path / ".env"
    env_file.write_text("KITE_API_KEY=test_api_key\nKITE_API_SECRET=test_api_secret\n")
    return EnvSecrets.for_environment(Environment.DEVELOPMENT, env_file=env_file, environ={})


def test_the_login_url_names_the_api_key(secrets: EnvSecrets) -> None:
    assert login_url_for(secrets) == "https://kite.trade/connect/login?v=3&api_key=test_api_key"


def test_a_request_token_is_exchanged_and_the_access_token_stored_for_today(
    secrets: EnvSecrets, tmp_path: Path
) -> None:
    result = login(
        secrets,
        PlaybackTransport(FIXTURES),
        request_token="test_request_token",
        today=TODAY,
    )

    assert result.user_id == "AB1234"
    reloaded = EnvSecrets.for_environment(
        Environment.DEVELOPMENT, env_file=tmp_path / ".env", environ={}
    )
    assert reloaded.get("KITE_ACCESS_TOKEN") == "test_access_token"
    assert reloaded.get("KITE_ACCESS_TOKEN_ISSUED_ON") == "2026-10-21"
    assert reloaded.get("KITE_API_KEY") == "test_api_key"  # other lines untouched


def test_a_rejected_request_token_is_an_error_and_nothing_is_stored(
    secrets: EnvSecrets,
) -> None:
    with pytest.raises(KiteError, match="TokenException"):
        login(secrets, PlaybackTransport(FIXTURES), request_token="bad_request_token", today=TODAY)

    assert secrets.get("KITE_ACCESS_TOKEN") is None


def test_login_without_api_credentials_fails_by_name() -> None:
    bare = EnvSecrets.for_environment(Environment.PRODUCTION, environ={})

    with pytest.raises(MissingSecret, match="KITE_API_KEY"):
        login(bare, PlaybackTransport(FIXTURES), request_token="x", today=TODAY)
