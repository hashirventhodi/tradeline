from pathlib import Path

import pytest

from tradeline.environment import Environment
from tradeline.transport import (
    BrowserTransport,
    LiveTransport,
    LiveTransportForbidden,
    PlaybackTransport,
    RecordingTransport,
    RefusingTransport,
    Request,
    Response,
    TransportRefused,
    UnrecordedRequest,
    transport_for,
)


class CannedTransport:
    """Stands in for the network: answers every request with the same bytes."""

    def __init__(self, response: Response) -> None:
        self.response = response

    def send(self, request: Request) -> Response:
        return self.response


@pytest.mark.parametrize("live", [LiveTransport, BrowserTransport])
def test_a_live_transport_cannot_be_constructed_under_the_test_environment(
    live: type[LiveTransport | BrowserTransport],
) -> None:
    with pytest.raises(LiveTransportForbidden):
        live()


def test_the_default_transport_under_test_refuses_every_call() -> None:
    transport = transport_for(Environment.TEST)

    assert isinstance(transport, RefusingTransport)
    with pytest.raises(TransportRefused, match="GET https://api.kite.trade/instruments"):
        transport.send(Request("GET", "https://api.kite.trade/instruments"))


def test_the_default_browser_transport_under_test_refuses_every_call() -> None:
    assert isinstance(transport_for(Environment.TEST, browser_tls=True), RefusingTransport)


def test_an_unrecorded_request_under_playback_fails_loudly(tmp_path: Path) -> None:
    playback = PlaybackTransport(tmp_path)

    with pytest.raises(UnrecordedRequest, match="GET https://www.nseindia.com/missing"):
        playback.send(Request("GET", "https://www.nseindia.com/missing"))


def test_recording_writes_a_fixture_that_playback_serves_byte_identically(
    tmp_path: Path,
) -> None:
    body = b"\xff\xfe binary \x00 and text \xe2\x82\xb9 and \r\n newlines"
    headers = (("content-type", "application/octet-stream"),)
    live = CannedTransport(Response(status=201, headers=headers, body=body))
    request = Request(
        "POST",
        "https://api.kite.trade/session/token?x=1",
        headers={"Authorization": "token secret"},
        body=b"api_key=k&request_token=r",
    )

    recorded = RecordingTransport(live, tmp_path).send(request)
    played = PlaybackTransport(tmp_path).send(request)

    assert recorded == Response(201, headers, body)
    assert played == recorded


def test_repeated_response_headers_survive_recording_in_order(tmp_path: Path) -> None:
    headers = (
        ("set-cookie", "nsit=abc; Path=/"),
        ("content-type", "text/html"),
        ("set-cookie", "nseappid=xyz; Path=/"),
    )
    live = CannedTransport(Response(200, headers, b""))
    request = Request("GET", "https://www.nseindia.com/")

    RecordingTransport(live, tmp_path).send(request)
    played = PlaybackTransport(tmp_path).send(request)

    assert played.headers == headers
    assert played.header_values("Set-Cookie") == ["nsit=abc; Path=/", "nseappid=xyz; Path=/"]
    assert played.header("Content-Type") == "text/html"
    assert played.header("x-missing") is None


def test_text_responses_are_stored_readably_and_round_trip(tmp_path: Path) -> None:
    body = b'{"status": "ok", "data": [1, 2]}\n'
    live = CannedTransport(Response(200, (("content-type", "application/json"),), body))
    request = Request("GET", "https://api.kite.trade/instruments")

    RecordingTransport(live, tmp_path).send(request)

    (fixture,) = tmp_path.glob("*.json")
    text = fixture.read_text()
    assert '"body_encoding": "utf-8"' in text
    assert "status" in text and "ok" in text
    assert PlaybackTransport(tmp_path).send(request).body == body


def test_request_headers_are_never_written_into_fixtures(tmp_path: Path) -> None:
    live = CannedTransport(Response(200, (), b"ok"))
    request = Request("GET", "https://x", headers={"Authorization": "token s3cret"})

    RecordingTransport(live, tmp_path).send(request)

    (fixture,) = tmp_path.glob("*.json")
    assert "s3cret" not in fixture.read_text()


def test_fixtures_are_keyed_by_method_url_and_body(tmp_path: Path) -> None:
    live = CannedTransport(Response(200, (), b"ok"))
    recording = RecordingTransport(live, tmp_path)
    recording.send(Request("GET", "https://x/a"))
    recording.send(Request("GET", "https://x/b"))
    recording.send(Request("POST", "https://x/a", body=b"1"))
    recording.send(Request("POST", "https://x/a", body=b"2"))

    assert len(list(tmp_path.glob("*.json"))) == 4
    playback = PlaybackTransport(tmp_path)
    assert playback.send(Request("POST", "https://x/a", body=b"2")).body == b"ok"
    with pytest.raises(UnrecordedRequest):
        playback.send(Request("DELETE", "https://x/a"))


def test_playback_keeps_a_log_of_the_requests_it_served(tmp_path: Path) -> None:
    live = CannedTransport(Response(200, (), b"ok"))
    RecordingTransport(live, tmp_path).send(Request("GET", "https://x/a"))
    playback = PlaybackTransport(tmp_path)

    playback.send(Request("GET", "https://x/a"))
    playback.send(Request("GET", "https://x/a"))

    assert playback.calls == [Request("GET", "https://x/a"), Request("GET", "https://x/a")]
