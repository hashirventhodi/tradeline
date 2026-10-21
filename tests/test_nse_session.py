"""``NseSession``: the headers, cookies, and backoff NSE's site requires.

The transport here is a hand-written fake rather than a fixture because
fixtures never record request headers, and request headers are the behaviour.
"""

from collections.abc import Callable

from tradeline.sources.nse import NseSession
from tradeline.transport import Request, Response

HOME = "https://www.nseindia.com/"
API = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"
ARCHIVE = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


class ScriptedTransport:
    """Answers each request with the next scripted responder; records what it was sent."""

    def __init__(self, *responders: Callable[[Request], Response]) -> None:
        self._responders = list(responders)
        self.sent: list[Request] = []

    def send(self, request: Request) -> Response:
        self.sent.append(request)
        return self._responders.pop(0)(request)


def ok(body: bytes = b"ok", *cookies: str) -> Callable[[Request], Response]:
    headers = tuple(("set-cookie", f"{cookie}; Path=/; Secure") for cookie in cookies)
    return lambda request: Response(200, headers, body)


def status(code: int) -> Callable[[Request], Response]:
    return lambda request: Response(code, (), b"")


def test_archive_downloads_carry_browser_headers_and_need_no_session() -> None:
    transport = ScriptedTransport(ok(b"csv"))

    response = NseSession(transport).get(ARCHIVE)

    assert response.body == b"csv"
    (request,) = transport.sent
    assert request.url == ARCHIVE
    assert request.headers["User-Agent"].startswith("Mozilla/5.0")
    assert request.headers["Accept-Language"] == "en-US,en;q=0.9"
    assert request.headers["Referer"] == HOME
    assert "Cookie" not in request.headers


def test_the_site_api_is_called_only_after_the_homepage_has_set_session_cookies() -> None:
    transport = ScriptedTransport(ok(b"", "nsit=abc", "nseappid=xyz"), ok(b'{"data": []}'))

    response = NseSession(transport).get(API)

    assert response.body == b'{"data": []}'
    warm_up, api_call = transport.sent
    assert warm_up.url == HOME
    assert api_call.url == API
    assert api_call.headers["Cookie"] == "nsit=abc; nseappid=xyz"


def test_cookies_persist_across_calls_and_the_homepage_is_fetched_once() -> None:
    transport = ScriptedTransport(ok(b"", "nsit=abc"), ok(), ok())
    session = NseSession(transport)

    session.get(API)
    session.get(API)

    assert [request.url for request in transport.sent] == [HOME, API, API]
    assert transport.sent[2].headers["Cookie"] == "nsit=abc"


def test_an_expired_session_is_renewed_once_and_the_call_repeated() -> None:
    transport = ScriptedTransport(
        ok(b"", "nsit=old"), status(401), ok(b"", "nsit=new"), ok(b"fresh")
    )

    response = NseSession(transport).get(API)

    assert response.body == b"fresh"
    assert [request.url for request in transport.sent] == [HOME, API, HOME, API]
    assert transport.sent[1].headers["Cookie"] == "nsit=old"
    assert transport.sent[3].headers["Cookie"] == "nsit=new"


def test_throttling_and_server_errors_back_off_then_retry() -> None:
    transport = ScriptedTransport(status(429), status(503), ok(b"finally"))
    delays: list[float] = []

    response = NseSession(transport, sleep=delays.append).get(ARCHIVE)

    assert response.body == b"finally"
    assert delays == [1.0, 3.0]
    assert len(transport.sent) == 3


def test_a_persistent_server_error_is_returned_after_the_last_retry() -> None:
    transport = ScriptedTransport(status(503), status(503), status(503), status(503))
    delays: list[float] = []

    response = NseSession(transport, sleep=delays.append).get(ARCHIVE)

    assert response.status == 503
    assert delays == [1.0, 3.0, 9.0]


def test_a_missing_file_is_a_plain_404_with_no_retry() -> None:
    transport = ScriptedTransport(status(404))
    delays: list[float] = []

    response = NseSession(transport, sleep=delays.append).get(ARCHIVE)

    assert response.status == 404
    assert delays == []
