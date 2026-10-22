"""Recorded responses played back with a deliberate change, so no fixture is edited by hand.

A test that needs a response the sources never sent (a missing bar, a changed
close, a reclassified symbol, a day narrowed out of a recorded range) plays the
real recording back through ``Playback`` with an ``ask`` that maps its request
onto the recorded one, an ``edit`` that changes the recorded body, or both.
"""

import io
import json
import re
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tradeline.sources.nse import bhavcopy_url
from tradeline.transport import PlaybackTransport, Request, Response, UnrecordedRequest

Ask = Callable[[Request], Request]
Edit = Callable[[Request, bytes], bytes]
Candle = list[Any]  # Kite's [timestamp, open, high, low, close, volume]
CandleEdit = Callable[[Request, list[Candle]], list[Candle]]


class Playback:
    """Serves each request from the first directory that recorded it.

    ``ask`` turns the request into the one that was recorded; ``edit`` is then
    applied to the recorded body, given the request as it was sent. Every
    request is noted in ``calls`` as it was sent.
    """

    def __init__(self, *dirs: Path, ask: Ask | None = None, edit: Edit | None = None) -> None:
        self._playbacks = [PlaybackTransport(d) for d in dirs]
        self._ask = ask
        self._edit = edit
        self.calls: list[Request] = []

    def send(self, request: Request) -> Response:
        self.calls.append(request)
        recorded = request if self._ask is None else self._ask(request)
        response = self._recorded(recorded)
        if self._edit is None:
            return response
        return Response(response.status, response.headers, self._edit(request, response.body))

    def _recorded(self, request: Request) -> Response:
        for playback in self._playbacks:
            try:
                return playback.send(request)
            except UnrecordedRequest:
                continue
        raise UnrecordedRequest(f"no fixture for {request.describe()} in any directory")


def recorded_range(start: str, end: str) -> Ask:
    """Ask Kite's historical endpoint for the recorded ``start`` to ``end`` instead."""

    def ask(request: Request) -> Request:
        parts = urlsplit(request.url)
        recorded = parts._replace(query=f"from={start}&to={end}").geturl()
        return Request(request.method, recorded, request.headers, request.body)

    return ask


def kite_candles(edit: CandleEdit) -> Edit:
    """An edit of the candles in a Kite historical response."""

    def edit_body(request: Request, body: bytes) -> bytes:
        document = json.loads(body)
        document["data"]["candles"] = edit(request, document["data"]["candles"])
        return json.dumps(document).encode()

    return edit_body


def in_requested_range(request: Request, candles: list[Candle]) -> list[Candle]:
    """The candles inside the range the request asked for, as Kite would have sent them."""
    query = parse_qs(urlsplit(request.url).query)
    start, end = query["from"][0], query["to"][0]
    return [c for c in candles if start <= c[0][:10] <= end]


def no_candles(request: Request, candles: list[Candle]) -> list[Candle]:
    return []


def lines(edit: Callable[[list[str]], list[str]]) -> Edit:
    """An edit of a text body, line by line."""

    def edit_body(request: Request, body: bytes) -> bytes:
        return "\n".join(edit(body.decode().split("\n"))).encode()

    return edit_body


def drop_bar(token: str, stamp: str) -> CandleEdit:
    """Instrument ``token``'s bars whose timestamp starts with ``stamp`` left out."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        return [c for c in candles if not c[0].startswith(stamp)]

    return edit


def shift_close(token: str, stamp: str, by: str) -> CandleEdit:
    """Instrument ``token``'s close moved by ``by`` rupees on the bars whose timestamp
    starts with ``stamp``, their high and low kept consistent."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        for candle in candles:
            if candle[0].startswith(stamp):
                candle[4] = float(Decimal(str(candle[4])) + Decimal(by))
                candle[2] = max(candle[2], candle[4])
                candle[3] = min(candle[3], candle[4])
        return candles

    return edit


def zero_prices(token: str, stamp: str) -> CandleEdit:
    """Instrument ``token``'s bars whose timestamp starts with ``stamp`` with every price
    0, as Kite serves a bar whose adjusted prices all round away."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        for candle in candles:
            if candle[0].startswith(stamp):
                candle[1:5] = [0, 0, 0, 0]
        return candles

    return edit


def affine_prices(token: str, times: str, minus: str) -> CandleEdit:
    """Instrument ``token``'s every price ``p`` replaced by ``p * times - minus``, as an
    additive adjustment of Kite's would leave it (#17)."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        for candle in candles:
            candle[1:5] = [
                float(Decimal(str(p)) * Decimal(times) - Decimal(minus)) for p in candle[1:5]
            ]
        return candles

    return edit


def whole_rupees(token: str, times: str) -> CandleEdit:
    """Instrument ``token``'s every price ``p`` replaced by ``p * times`` rounded to the
    rupee, half to even, as Kite rounds some adjusted histories (#17)."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        for candle in candles:
            candle[1:5] = [
                float((Decimal(str(p)) * Decimal(times)).quantize(Decimal(1), ROUND_HALF_EVEN))
                for p in candle[1:5]
            ]
        return candles

    return edit


def add_bar(token: str, bar: Candle) -> CandleEdit:
    """Instrument ``token``'s bars with ``bar`` added, in timestamp order."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        if f"/historical/{token}/" not in request.url:
            return candles
        return sorted([*candles, bar], key=lambda c: c[0])

    return edit


def then(*edits: CandleEdit) -> CandleEdit:
    """The candle edits applied in turn."""

    def edit(request: Request, candles: list[Candle]) -> list[Candle]:
        for each in edits:
            candles = each(request, candles)
        return candles

    return edit


def nse_corporate_actions_from(from_date: str, to_date: str) -> Ask:
    """Ask each of NSE's corporate-action lists for the recorded ``from_date`` to
    ``to_date`` (``DD-MM-YYYY``) instead; every other request is asked as sent."""

    def ask(request: Request) -> Request:
        parts = urlsplit(request.url)
        if parts.path != "/api/corporates-corporateActions":
            return request
        (nse_list,) = parse_qs(parts.query)["index"]
        query = f"index={nse_list}&from_date={from_date}&to_date={to_date}"
        return Request(request.method, parts._replace(query=query).geturl(), request.headers, None)

    return ask


def corporate_actions_in_requested_range(request: Request, body: bytes) -> bytes:
    """NSE's corporate actions with an Ex-Date in the range the request asked for, as NSE
    would have sent them (none of those with no Ex-Date); every other body as recorded."""
    parts = urlsplit(request.url)
    if parts.path != "/api/corporates-corporateActions":
        return body
    query = parse_qs(parts.query)
    start, end = (
        datetime.strptime(query[name][0], "%d-%m-%Y").date() for name in ("from_date", "to_date")
    )
    records = [
        r
        for r in json.loads(body)
        if r["exDate"] != "-" and start <= datetime.strptime(r["exDate"], "%d-%b-%Y").date() <= end
    ]
    return json.dumps(records).encode()


def bhavcopy_as_of(day: date, recorded: date) -> tuple[Ask, Edit]:
    """NSE's end-of-day file for ``recorded`` served as ``day``'s, its dates moved to ``day``.

    Both days must be in the same file format (UDiFF from 2024-07-08). Every
    other request is asked and served as recorded.
    """
    url = bhavcopy_url(day)

    def ask(request: Request) -> Request:
        if request.url != url:
            return request
        return Request(request.method, bhavcopy_url(recorded), request.headers, request.body)

    def edit(request: Request, body: bytes) -> bytes:
        if request.url != url:
            return body
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            (name,) = archive.namelist()
            text = archive.read(name).decode()
        moved = io.BytesIO()
        with zipfile.ZipFile(moved, "w") as archive:
            archive.writestr(name, text.replace(recorded.isoformat(), day.isoformat()))
        return moved.getvalue()

    return ask, edit


def bhavcopy_series(day: date, symbol: str, series: str | None) -> Edit:
    """NSE's end-of-day file for ``day`` with ``symbol``'s row moved to ``series``, or
    left out where ``series`` is None, as a day NSE lists it elsewhere or not at all.

    The file must be in UDiFF, where the series follows the Symbol. Every other
    body is served as recorded.
    """
    url = bhavcopy_url(day)

    def edit(request: Request, body: bytes) -> bytes:
        if request.url != url:
            return body
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            (name,) = archive.namelist()
            rows = archive.read(name).decode().split("\n")
        kept = []
        for row in rows:
            if f",{symbol}," not in row:
                kept.append(row)
            elif series is not None:
                before, after = row.split(f",{symbol},", 1)
                kept.append(f"{before},{symbol},{series},{after.split(',', 1)[1]}")
        moved = io.BytesIO()
        with zipfile.ZipFile(moved, "w") as archive:
            archive.writestr(name, "\n".join(kept))
        return moved.getvalue()

    return edit


def bhavcopy_rows(edit: Callable[[date, list[str]], list[str]]) -> Edit:
    """An edit of the CSV lines of each UDiFF end-of-day file (from 2024-07-08), given its day.

    Every other body, and a day NSE has no file for, is served as recorded.
    """

    def edit_body(request: Request, body: bytes) -> bytes:
        found = _UDIFF_DAY.search(request.url)
        if found is None or not zipfile.is_zipfile(io.BytesIO(body)):
            return body
        day = datetime.strptime(found.group(1), "%Y%m%d").date()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            (name,) = archive.namelist()
            text = archive.read(name).decode()
        edited = io.BytesIO()
        with zipfile.ZipFile(edited, "w") as archive:
            archive.writestr(name, "\n".join(edit(day, text.split("\n"))))
        return edited.getvalue()

    return edit_body


_UDIFF_DAY = re.compile(r"/BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv\.zip$")
