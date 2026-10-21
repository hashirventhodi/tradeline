"""Kite Connect: the instrument dump, the login flow, and historical candles.

``GET /instruments/NSE`` is a public CSV (no access token) listing every NSE
instrument Kite can trade with its ``instrument_token``, the identifier the
historical and streaming APIs key on. Equities are ``segment == "NSE"`` and
``instrument_type == "EQ"`` (indices sit in segment ``INDICES``).

Kite's ``tradingsymbol`` is NSE's symbol for the ``EQ`` series and
``SYMBOL-SERIES`` for every other series. The reverse is ambiguous because NSE
symbols may themselves contain hyphens (``BAJAJ-AUTO``), so this module never
splits a trading symbol; callers build the expected trading symbol from a known
(symbol, series) with ``trading_symbol`` and look it up.

Everything else needs the day's access token (see ``login_url`` and
``exchange_request_token``). ``KiteSession`` owns the authenticated calls: the
token header, the historical API's limit of 3 requests per second, and
exponential backoff on 429 and 5xx. The historical API also caps the span of
one request by interval (``MAX_DAYS_PER_REQUEST``); ``candle_ranges`` chunks a
range to fit and ``fetch_candles`` walks the chunks. A bar is stamped with the
time it starts in Indian time: midnight for a daily bar.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from tradeline.transport import Request, Response, Transport

log = logging.getLogger(__name__)

API_HOST = "https://api.kite.trade"
INSTRUMENTS_URL = f"{API_HOST}/instruments/NSE"
LOGIN_URL = "https://kite.trade/connect/login"
SESSION_TOKEN_URL = f"{API_HOST}/session/token"
HISTORICAL_URL = f"{API_HOST}/instruments/historical"
KITE_HEADERS: dict[str, str] = {"X-Kite-Version": "3"}

# The historical API's limits, a span counted in days inclusive of both ends.
# For minute bars Kite answered 1 July to 30 August 2024 (61 days inclusive)
# and refused 1 July to 31 August ("interval exceeds max limit: 60 days");
# 60 inclusive keeps a day's margin.
REQUESTS_PER_SECOND = 3.0
INTERVAL_DAY = "day"
INTERVAL_MINUTE = "minute"
MAX_DAYS_PER_REQUEST: dict[str, int] = {INTERVAL_DAY: 2000, INTERVAL_MINUTE: 60}

# Exponential backoff on throttling (429) and server errors: the delay before each retry.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)


class KiteError(RuntimeError):
    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


# --- Instrument dump --------------------------------------------------------


@dataclass(frozen=True)
class KiteInstrument:
    instrument_token: int
    trading_symbol: str


def trading_symbol(symbol: str, series: str) -> str:
    return symbol if series == "EQ" else f"{symbol}-{series}"


def fetch_instruments(transport: Transport) -> list[KiteInstrument]:
    """Every NSE equity instrument in Kite's dump."""
    response = transport.send(Request("GET", INSTRUMENTS_URL, headers=KITE_HEADERS))
    if response.status != 200:
        raise KiteError(f"HTTP {response.status} fetching {INSTRUMENTS_URL}")
    return parse_instruments(response.body)


def parse_instruments(body: bytes) -> list[KiteInstrument]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    return [
        KiteInstrument(
            instrument_token=int(record["instrument_token"]),
            trading_symbol=record["tradingsymbol"].strip(),
        )
        for record in reader
        if record["segment"] == "NSE" and record["instrument_type"] == "EQ"
    ]


# --- Login ------------------------------------------------------------------


@dataclass(frozen=True)
class AccessToken:
    access_token: str
    user_id: str
    login_time: str


def login_url(api_key: str) -> str:
    """Where the Operator logs in; Kite redirects back with a ``request_token``."""
    return f"{LOGIN_URL}?{urlencode({'v': 3, 'api_key': api_key})}"


def exchange_request_token(
    transport: Transport, *, api_key: str, api_secret: str, request_token: str
) -> AccessToken:
    """Trade the request token from the login redirect for the day's access token."""
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    body = urlencode({"api_key": api_key, "request_token": request_token, "checksum": checksum})
    response = transport.send(
        Request(
            "POST",
            SESSION_TOKEN_URL,
            headers={**KITE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            body=body.encode(),
        )
    )
    data = _data(response, SESSION_TOKEN_URL)
    return AccessToken(
        access_token=str(data["access_token"]),
        user_id=str(data["user_id"]),
        login_time=str(data["login_time"]),
    )


# --- Authenticated session --------------------------------------------------


class KiteSession:
    """The token header, the request rate limit, and backoff for every authenticated call."""

    def __init__(
        self,
        transport: Transport,
        *,
        api_key: str,
        access_token: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._headers = {**KITE_HEADERS, "Authorization": f"token {api_key}:{access_token}"}
        self._clock = clock
        self._sleep = sleep
        self._min_interval = 1.0 / REQUESTS_PER_SECOND
        self._last_sent_at: float | None = None
        self.requests_sent = 0

    def get_json(self, url: str) -> Any:
        """GET ``url`` and return Kite's JSON envelope; a Kite error is raised by type."""
        response = self._send(url)
        for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
            if not _is_throttled_or_failing(response.status):
                break
            log.warning(
                "kite throttled or failing; backing off",
                extra={"url": url, "status": response.status, "attempt": attempt, "delay": delay},
            )
            self._sleep(delay)
            response = self._send(url)
        return _envelope(response, url)

    def _send(self, url: str) -> Response:
        if self._last_sent_at is not None:
            wait = self._last_sent_at + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_sent_at = self._clock()
        self.requests_sent += 1
        return self._transport.send(Request("GET", url, headers=self._headers))


def _is_throttled_or_failing(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _envelope(response: Response, url: str) -> Any:
    try:
        document = json.loads(response.body, parse_float=Decimal)
    except ValueError:
        document = None
    succeeded = isinstance(document, dict) and document.get("status") == "success"
    if response.status == 200 and succeeded:
        return document
    error_type = document.get("error_type") if isinstance(document, dict) else None
    message = document.get("message") if isinstance(document, dict) else None
    text = f"HTTP {response.status} from {url}"
    if error_type or message:
        text += f": {error_type}: {message}"
    if error_type == "TokenException":
        text += " (the access token is missing or expired; run `tradeline login`)"
    raise KiteError(text, error_type=error_type)


def _data(response: Response, url: str) -> dict[str, Any]:
    data = _envelope(response, url)["data"]
    if not isinstance(data, dict):
        raise KiteError(f"unexpected response shape from {url}")
    return data


# --- Historical candles -----------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """One bar; ``ts`` is when it starts, with Kite's +05:30 offset."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @property
    def trade_date(self) -> date:
        """The Indian calendar day the bar belongs to."""
        return self.ts.date()


def historical_url(instrument_token: int, interval: str, start: date, end: date) -> str:
    query = urlencode({"from": start.isoformat(), "to": end.isoformat()})
    return f"{HISTORICAL_URL}/{instrument_token}/{interval}?{query}"


def candle_ranges(start: date, end: date, *, interval: str) -> list[tuple[date, date]]:
    """Split ``start``..``end`` (inclusive) into spans the historical API accepts."""
    span = timedelta(days=MAX_DAYS_PER_REQUEST[interval] - 1)
    ranges: list[tuple[date, date]] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + span, end)
        ranges.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return ranges


def fetch_candles(
    session: KiteSession, instrument_token: int, *, interval: str, start: date, end: date
) -> list[Candle]:
    """Raw candles for one instrument, oldest first, exactly as Kite returned them.

    A date-only ``to`` covers that whole day for minute bars too: 31 July to
    1 August 2024 returned both days' 375 bars each.
    """
    candles: list[Candle] = []
    for chunk_start, chunk_end in candle_ranges(start, end, interval=interval):
        url = historical_url(instrument_token, interval, chunk_start, chunk_end)
        candles.extend(parse_candles(session.get_json(url)))
    return candles


def parse_candles(document: Any) -> list[Candle]:
    rows = document["data"]["candles"]
    return [
        Candle(
            ts=datetime.strptime(str(row[0]), "%Y-%m-%dT%H:%M:%S%z"),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=int(row[5]),
        )
        for row in rows
    ]
