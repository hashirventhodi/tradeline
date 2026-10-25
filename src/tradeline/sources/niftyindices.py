"""niftyindices.com: NSE Indices' published Total Return Index history (ADR 0020).

One public endpoint, needing no login: ``TRI_URL``, which the site's
historical-data page (``/reports/historical-data``, "Total Returns Index")
posts to. Its body is JSON whose ``cinfo`` is a string in single quotes naming
the index (``name`` and ``indexName``, both the index's upper-case name) and a
date range (``startDate``, ``endDate``, as ``01-Apr-2005``). It answers a JSON
list, newest first, of ``{"Index Name", "Date" ("29 Apr 2005"),
"TotalReturnsIndex", ...}``; ``RequestNumber`` and ``NTR_Value`` are not read.
The page refuses a range of more than 365 days, so ``tri_windows`` splits a
range into windows from April to March, and ``NiftyIndicesSession`` sends at
most one request every ``MIN_INTERVAL_SECONDS``, backing off on 429 and server
errors. The older ``/Backpage.aspx/getTotalReturnIndexString`` path redirects
to a login.

Checked against the live endpoint on 2026-10-25 (``docs/research.md``,
"Relative Drawdown threshold (#97)").
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from tradeline.sources.browser import USER_AGENT, send_with_backoff
from tradeline.transport import Request, Response, Transport

SITE = "https://www.niftyindices.com"
TRI_URL = f"{SITE}/BackPage/getTotalReturnIndexString"
HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json; charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{SITE}/reports/historical-data",
}
# The same spacing as AMFI's; the site was not seen to throttle.
MIN_INTERVAL_SECONDS = 4.0
# The page's own limit on a range.
MAX_WINDOW_DAYS = 365


class NiftyIndicesError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexValue:
    """One day's close of a Total Return Index, as niftyindices.com publishes it."""

    index_name: str
    day: date
    value: Decimal


class NiftyIndicesSession:
    """The request spacing and backoff for every niftyindices.com call."""

    def __init__(
        self,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._last_sent_at: float | None = None

    def post(self, url: str, body: bytes) -> Response:
        if self._last_sent_at is not None:
            wait = self._last_sent_at + MIN_INTERVAL_SECONDS - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            return send_with_backoff(
                self._transport,
                Request("POST", url, headers=HEADERS, body=body),
                sleep=self._sleep,
                site="niftyindices",
            )
        finally:
            self._last_sent_at = self._clock()


def tri_windows(start: date, end: date) -> list[tuple[date, date]]:
    """``start`` to ``end``, inclusive, cut at each April 1st, so no window is over a year."""
    windows = []
    first = start
    while first <= end:
        april = date(first.year + (first.month >= 4), 4, 1)
        last = min(end, april - timedelta(days=1))
        windows.append((first, last))
        first = last + timedelta(days=1)
    return windows


def tri_request_body(index_name: str, start: date, end: date) -> bytes:
    """The body the historical-data page posts for ``index_name`` from ``start`` to ``end``."""
    if (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"{start} to {end} is more than {MAX_WINDOW_DAYS} days")
    cinfo = (
        f"{{'name':'{index_name}','startDate':'{start:%d-%b-%Y}',"
        f"'endDate':'{end:%d-%b-%Y}','indexName':'{index_name}'}}"
    )
    return json.dumps({"cinfo": cinfo}).encode()


def fetch_total_return_index(
    session: NiftyIndicesSession, index_name: str, start: date, end: date
) -> list[IndexValue]:
    """Every close niftyindices.com publishes for ``index_name``'s Total Return Index from
    ``start`` to ``end``, a window a call, oldest first."""
    values: list[IndexValue] = []
    for first, last in tri_windows(start, end):
        response = session.post(TRI_URL, tri_request_body(index_name, first, last))
        if response.status != 200:
            raise NiftyIndicesError(
                f"niftyindices.com answered HTTP {response.status} for {index_name}"
                f" from {first} to {last}"
            )
        values.extend(parse_total_return_index(index_name, response.body))
    return sorted(values, key=lambda v: v.day)


def parse_total_return_index(index_name: str, body: bytes) -> list[IndexValue]:
    """The closes in one answer; a row naming another index refuses it."""
    try:
        rows = json.loads(body)
    except ValueError as error:
        raise NiftyIndicesError(
            f"{index_name}: not a Total Return Index answer: {error}"
        ) from error
    if not isinstance(rows, list):
        raise NiftyIndicesError(f"{index_name}: not a Total Return Index answer: {rows!r:.80}")
    values = []
    for row in rows:
        try:
            named, day_text, value_text = row["Index Name"], row["Date"], row["TotalReturnsIndex"]
            day = datetime.strptime(day_text, "%d %b %Y").date()
            value = Decimal(value_text.replace(",", ""))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise NiftyIndicesError(f"{index_name}: unreadable row {row!r}: {error}") from error
        if named.upper() != index_name.upper():
            raise NiftyIndicesError(f"{index_name}: the answer names {named!r}")
        if value <= 0:
            raise NiftyIndicesError(f"{index_name}: {day} has the value {value}")
        values.append(IndexValue(index_name, day, value))
    return values
