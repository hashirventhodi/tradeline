"""``KiteSession``: the auth header, the 3-requests-per-second limit, and backoff.

The transport is a hand-written fake because fixtures never record request
headers, and the header and the spacing between requests are the behaviour.
Time is a fake clock that advances only when the session sleeps.
"""

from collections.abc import Callable
from datetime import date
from itertools import pairwise

import pytest

from tradeline.sources.kite import KiteError, KiteSession, candle_ranges
from tradeline.transport import Request, Response

URL = "https://api.kite.trade/instruments/historical/408065/day?from=2024-07-01&to=2024-07-05"
CANDLES = (
    b'{"status":"success","data":{"candles":['
    b'["2024-07-01T00:00:00+0530",1559.5,1599.9,1559.5,1590.8,6801771],'
    b'["2024-07-02T00:00:00+0530",1590,1599,1571.3,1575.9,5209106]]}}'
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedTransport:
    """Answers each request with the next scripted responder; records what it was sent, and when."""

    def __init__(self, clock: FakeClock, *responders: Callable[[Request], Response]) -> None:
        self._clock = clock
        self._responders = list(responders)
        self.sent: list[Request] = []
        self.sent_at: list[float] = []

    def send(self, request: Request) -> Response:
        self.sent.append(request)
        self.sent_at.append(self._clock.now)
        return self._responders.pop(0)(request)


def ok(body: bytes = CANDLES) -> Callable[[Request], Response]:
    return lambda request: Response(200, (("content-type", "application/json"),), body)


def status(code: int, body: bytes = b"") -> Callable[[Request], Response]:
    return lambda request: Response(code, (), body)


def session(transport: ScriptedTransport, clock: FakeClock) -> KiteSession:
    return KiteSession(
        transport, api_key="key", access_token="tok", clock=clock.time, sleep=clock.sleep
    )


def test_every_call_carries_the_token_header_and_the_kite_version() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(clock, ok())

    data = session(transport, clock).get_json(URL)

    assert data["data"]["candles"][0][0] == "2024-07-01T00:00:00+0530"
    (request,) = transport.sent
    assert request.headers["Authorization"] == "token key:tok"
    assert request.headers["X-Kite-Version"] == "3"


def test_calls_are_spaced_to_at_most_three_per_second() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(clock, ok(), ok(), ok(), ok())
    kite = session(transport, clock)

    for _ in range(4):
        kite.get_json(URL)

    gaps = [b - a for a, b in pairwise(transport.sent_at)]
    assert all(gap >= 1 / 3 - 1e-9 for gap in gaps)
    assert all(gap < 0.5 for gap in gaps)  # waits only as long as the limit needs


def test_no_wait_is_inserted_when_enough_time_has_already_passed() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(clock, ok(), ok())
    kite = session(transport, clock)

    kite.get_json(URL)
    clock.now += 2.0
    kite.get_json(URL)

    assert clock.sleeps == []


def test_throttling_and_server_errors_back_off_exponentially_then_retry() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(clock, status(429), status(503), status(520), ok())

    data = session(transport, clock).get_json(URL)

    assert data["status"] == "success"
    assert len(transport.sent) == 4
    assert [s for s in clock.sleeps if s >= 1.0] == [1.0, 2.0, 4.0]


def test_a_persistent_server_error_is_raised_after_the_last_retry() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(clock, *(status(503) for _ in range(10)))

    with pytest.raises(KiteError, match="HTTP 503"):
        session(transport, clock).get_json(URL)

    assert len(transport.sent) == 5  # the first try and four retries


def test_an_expired_token_is_a_clear_error_that_names_the_login_command() -> None:
    clock = FakeClock()
    body = (
        b'{"status":"error","message":"Incorrect `api_key` or `access_token`.",'
        b'"error_type":"TokenException"}'
    )
    transport = ScriptedTransport(clock, status(403, body))

    with pytest.raises(KiteError, match="TokenException.*tradeline login"):
        session(transport, clock).get_json(URL)

    assert clock.sleeps == []


def test_daily_ranges_are_chunked_to_at_most_two_thousand_days_each() -> None:
    assert candle_ranges(date(2024, 7, 1), date(2024, 7, 1), interval="day") == [
        (date(2024, 7, 1), date(2024, 7, 1))
    ]
    assert candle_ranges(date(2019, 1, 1), date(2024, 8, 30), interval="day") == [
        (date(2019, 1, 1), date(2024, 6, 22)),  # 2000 days inclusive
        (date(2024, 6, 23), date(2024, 8, 30)),
    ]
