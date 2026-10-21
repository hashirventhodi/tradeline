"""The one boundary to the outside world.

Every source adapter makes HTTP calls through a ``Transport``. There are five:

- ``LiveTransport``: real HTTP. Refuses to be constructed under the test environment.
- ``BrowserTransport``: real HTTP with a browser's TLS handshake, which BSE's API
  requires: it refuses httpx and curl outright, whatever their headers. Refused
  under test too.
- ``PlaybackTransport``: serves recorded fixtures; an unrecorded request raises.
- ``RecordingTransport``: wraps another transport and writes what it sees as fixtures.
- ``RefusingTransport``: raises on any call; the default under test.

Fixtures are one JSON file per (method, url, body). Request headers are never
recorded because they carry credentials; response headers and body are kept
byte-for-byte.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from curl_cffi import requests as curl_requests

from tradeline.environment import Environment

DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None

    def describe(self) -> str:
        return f"{self.method} {self.url}"


@dataclass(frozen=True)
class Response:
    status: int
    # Ordered (name, value) pairs so repeated headers such as Set-Cookie survive.
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        """The first value of ``name``, case-insensitively, or None."""
        values = self.header_values(name)
        return values[0] if values else None

    def header_values(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


class Transport(Protocol):
    def send(self, request: Request) -> Response: ...


class LiveTransportForbidden(RuntimeError):
    pass


class TransportRefused(RuntimeError):
    pass


class UnrecordedRequest(LookupError):
    pass


def _refuse_under_test(name: str) -> None:
    if Environment.current() is Environment.TEST:
        raise LiveTransportForbidden(
            f"the {name} cannot be used under TRADELINE_ENV=test; "
            "use PlaybackTransport with recorded fixtures"
        )


class LiveTransport:
    """Real HTTP through httpx."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        _refuse_under_test("live transport")
        self._client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def send(self, request: Request) -> Response:
        raw = self._client.request(
            request.method, request.url, headers=dict(request.headers), content=request.body
        )
        return Response(
            status=raw.status_code, headers=tuple(raw.headers.multi_items()), body=raw.content
        )


class BrowserTransport:
    """Real HTTP with a browser's TLS handshake (curl_cffi impersonating Chrome)."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        _refuse_under_test("browser transport")
        self._timeout = timeout_seconds
        # Cookies are the sources' business (see NseSession), so this session keeps none.
        self._session: curl_requests.Session[curl_requests.Response] = curl_requests.Session(
            impersonate="chrome", allow_redirects=False
        )

    def send(self, request: Request) -> Response:
        self._session.cookies.clear()
        answer = self._session.request(
            request.method,  # type: ignore[arg-type]
            request.url,
            headers=dict(request.headers),
            data=request.body,
            timeout=self._timeout,
        )
        return Response(
            status=answer.status_code,
            headers=tuple((k, v or "") for k, v in answer.headers.multi_items()),
            body=answer.content,
        )


class RefusingTransport:
    def send(self, request: Request) -> Response:
        raise TransportRefused(
            f"no transport was injected; refusing network call {request.describe()}"
        )


class PlaybackTransport:
    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir
        self.calls: list[Request] = []

    def send(self, request: Request) -> Response:
        self.calls.append(request)
        path = _fixture_path(self.fixture_dir, request)
        if not path.exists():
            raise UnrecordedRequest(
                f"no fixture for {request.describe()} under {self.fixture_dir} "
                f"(expected {path.name}); record it with RecordingTransport"
            )
        return _read_fixture(path)


class RecordingTransport:
    def __init__(self, inner: Transport, fixture_dir: Path) -> None:
        self._inner = inner
        self.fixture_dir = fixture_dir

    def send(self, request: Request) -> Response:
        response = self._inner.send(request)
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        _write_fixture(_fixture_path(self.fixture_dir, request), request, response)
        return response


def transport_for(environment: Environment, *, browser_tls: bool = False) -> Transport:
    """The default transport when none is injected: live, except under test."""
    if environment is Environment.TEST:
        return RefusingTransport()
    return BrowserTransport() if browser_tls else LiveTransport()


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _fixture_path(fixture_dir: Path, request: Request) -> Path:
    digest = hashlib.sha256()
    digest.update(request.method.upper().encode())
    digest.update(b"\n")
    digest.update(request.url.encode())
    digest.update(b"\n")
    digest.update(request.body or b"")
    parts = urlsplit(request.url)
    slug = _UNSAFE.sub("_", f"{parts.netloc}{parts.path}").strip("_")[:80]
    return fixture_dir / f"{request.method.upper()}_{slug}_{digest.hexdigest()[:12]}.json"


def _write_fixture(path: Path, request: Request, response: Response) -> None:
    request_body, request_encoding = (
        (None, None) if request.body is None else _encode_body(request.body)
    )
    response_body, response_encoding = _encode_body(response.body)
    document = {
        "request": {
            "method": request.method.upper(),
            "url": request.url,
            "body": request_body,
            "body_encoding": request_encoding,
        },
        "response": {
            "status": response.status,
            "headers": [list(pair) for pair in response.headers],
            "body": response_body,
            "body_encoding": response_encoding,
        },
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def _read_fixture(path: Path) -> Response:
    document = json.loads(path.read_text())
    response = document["response"]
    return Response(
        status=int(response["status"]),
        headers=tuple((str(name), str(value)) for name, value in response["headers"]),
        body=_decode_body(response["body"], response["body_encoding"]),
    )


def _encode_body(body: bytes) -> tuple[str, str]:
    """Store UTF-8 bodies as text so fixtures are reviewable; anything else as base64."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(body).decode("ascii"), "base64"
    if text.encode("utf-8") == body and "\r" not in text:
        return text, "utf-8"
    return base64.b64encode(body).decode("ascii"), "base64"


def _decode_body(text: str, encoding: str) -> bytes:
    if encoding == "utf-8":
        return text.encode("utf-8")
    if encoding == "base64":
        return base64.b64decode(text)
    raise ValueError(f"unknown fixture body encoding {encoding!r}")
