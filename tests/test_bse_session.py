"""``BseSession``: the headers and backoff BSE's API requires.

As for ``NseSession``, the transport records what it was sent, because
fixtures never record request headers, and request headers are the
behaviour. It answers with BSE's recorded response for 1 August 2024's first
page (``fixtures/collect_daily/``) under each scripted status in turn.
"""

from datetime import date
from pathlib import Path

from tradeline.sources.bse import BseSession, announcements_url
from tradeline.transport import PlaybackTransport, Request, Response

FIXTURES = Path(__file__).parent / "fixtures" / "collect_daily"
API = announcements_url(date(2024, 8, 1), 1)


class ScriptedTransport:
    """Answers with the recorded response under the scripted statuses in turn."""

    def __init__(self, *statuses: int) -> None:
        self._statuses = list(statuses)
        self._recorded = PlaybackTransport(FIXTURES)
        self.sent: list[Request] = []

    def send(self, request: Request) -> Response:
        self.sent.append(request)
        recorded = self._recorded.send(request)
        return Response(self._statuses.pop(0), recorded.headers, recorded.body)


def test_every_call_carries_the_site_as_referer_and_origin() -> None:
    transport = ScriptedTransport(200)

    BseSession(transport).get(API)

    (request,) = transport.sent
    assert request.headers["User-Agent"].startswith("Mozilla/5.0")
    assert request.headers["Referer"] == "https://www.bseindia.com/"
    assert request.headers["Origin"] == "https://www.bseindia.com"


def test_throttling_and_server_errors_are_retried_with_growing_delays() -> None:
    transport = ScriptedTransport(429, 503, 200)
    slept: list[float] = []

    response = BseSession(transport, sleep=slept.append).get(API)

    assert response.status == 200
    assert len(transport.sent) == 3
    assert slept == [1.0, 3.0]


def test_a_refusal_is_returned_not_retried() -> None:
    transport = ScriptedTransport(403)

    response = BseSession(transport, sleep=lambda _: None).get(API)

    assert response.status == 403
    assert len(transport.sent) == 1
