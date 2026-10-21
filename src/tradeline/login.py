"""``login``: the daily Kite Session Login, by hand.

The Operator opens the login URL, completes Kite's login and second factor,
and is redirected to ``KITE_REDIRECT_URL`` with a ``request_token`` in the
query string. That token, pasted back, is exchanged for the day's access
token, which is stored through the secrets provider with the date it was
issued. Phase 3 replaces the paste with the Telegram flow (ADR 0010); the
storage interface stays the same.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from tradeline.secrets import (
    KITE_ACCESS_TOKEN,
    KITE_ACCESS_TOKEN_ISSUED_ON,
    KITE_API_KEY,
    KITE_API_SECRET,
    SecretsProvider,
)
from tradeline.sources import kite
from tradeline.transport import Transport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginResult:
    user_id: str
    issued_on: date


def login_url_for(secrets: SecretsProvider) -> str:
    return kite.login_url(secrets.require(KITE_API_KEY))


def login(
    secrets: SecretsProvider, transport: Transport, *, request_token: str, today: date
) -> LoginResult:
    """Exchange ``request_token`` for an access token and store it as today's."""
    api_key = secrets.require(KITE_API_KEY)
    api_secret = secrets.require(KITE_API_SECRET)
    token = kite.exchange_request_token(
        transport, api_key=api_key, api_secret=api_secret, request_token=request_token.strip()
    )
    secrets.put(KITE_ACCESS_TOKEN, token.access_token)
    secrets.put(KITE_ACCESS_TOKEN_ISSUED_ON, today.isoformat())
    log.info(
        "kite access token stored",
        extra={"user_id": token.user_id, "login_time": token.login_time, "issued_on": today},
    )
    return LoginResult(user_id=token.user_id, issued_on=today)
