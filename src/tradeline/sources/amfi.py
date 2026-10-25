"""AMFI: mutual fund schemes and their published NAVs (ADR 0015 amendment).

Two public endpoints, neither needing a login:

- The scheme master, ``NAVAll.txt`` (``SCHEME_MASTER_URL``): every open scheme's
  latest NAV, one ``;``-separated line per scheme code, with its ISINs, scheme
  name, plan (``Direct Plan``, ``Regular Plan``) and option (``Growth``,
  ``IDCW``...). Lines that are not a scheme (the header, fund-house and
  category headings, blank lines) are skipped. AMFI's other master,
  ``DownloadSchemeData_Po.aspx``, names 147625 only "Motilal Oswal Nifty 500
  Index Fund - Direct Plan", with no option, so this one is read.
- The NAV history API (``NAV_HISTORY_URL``): one scheme's NAVs over a date
  range, each with AMFI's upload time. It refuses a range of five years or more
  (HTTP 400) and answers HTTP 429 when called fast, so ``nav_windows`` splits a
  range into four-year windows and ``AmfiSession`` sends at most one request
  every ``MIN_INTERVAL_SECONDS``, backing off on 429 and server errors.

Checked against the live endpoints on 2026-10-25 (``docs/research.md``,
"Benchmark, index method and demerger facts").
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from tradeline.sources.browser import USER_AGENT, send_with_backoff
from tradeline.transport import Request, Response, Transport

SCHEME_MASTER_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY_URL = "https://www.amfiindia.com/api/nav-history"
# About 4 s between calls sufficed on 2026-10-25; faster drew HTTP 429.
MIN_INTERVAL_SECONDS = 4.0
# Five years or more is refused; four years is the window.
WINDOW_YEARS = 4
HEADERS: dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "*/*"}
_MASTER_FIELDS = 8


class AmfiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemeRecord:
    """One scheme code's line in the scheme master."""

    scheme_code: int
    isin_growth: str | None
    isin_reinvestment: str | None
    scheme_name: str
    plan: str
    option: str
    # The date of the NAV the line carries.
    nav_date: date

    @property
    def full_name(self) -> str:
        """The scheme name with its plan and option, as AMFI names the line."""
        return " ".join(part for part in (self.scheme_name, self.plan, self.option) if part)


@dataclass(frozen=True)
class NavRecord:
    scheme_code: int
    nav_date: date
    nav: Decimal
    # AMFI's upload time as its clock shows it. It is suffixed "Z" but displayed
    # as the same clock time and falls in the Indian evening, so it is kept
    # without a zone rather than read as UTC. None where AMFI gives none.
    uploaded_at: datetime | None


class AmfiSession:
    """The request spacing and backoff for every AMFI call."""

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

    def get(self, url: str) -> Response:
        if self._last_sent_at is not None:
            wait = self._last_sent_at + MIN_INTERVAL_SECONDS - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            return send_with_backoff(
                self._transport,
                Request("GET", url, headers=HEADERS),
                sleep=self._sleep,
                site="amfi",
            )
        finally:
            self._last_sent_at = self._clock()


def fetch_scheme_master(session: AmfiSession) -> list[SchemeRecord]:
    response = session.get(SCHEME_MASTER_URL)
    if response.status != 200:
        raise AmfiError(f"AMFI's scheme master answered HTTP {response.status}")
    return parse_scheme_master(response.body.decode("utf-8-sig"))


def parse_scheme_master(text: str) -> list[SchemeRecord]:
    """Every scheme line of ``NAVAll.txt``, in file order."""
    records = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(";")]
        if len(fields) != _MASTER_FIELDS or not fields[0].isdigit():
            continue
        code, growth, reinvestment, name, plan, option, _nav, day = fields
        try:
            nav_date = datetime.strptime(day, "%d-%b-%Y").date()
        except ValueError as error:
            raise AmfiError(f"scheme {code}: unreadable NAV date {day!r}") from error
        records.append(
            SchemeRecord(
                scheme_code=int(code),
                isin_growth=_isin(growth),
                isin_reinvestment=_isin(reinvestment),
                scheme_name=name,
                plan=plan,
                option=option,
                nav_date=nav_date,
            )
        )
    if not records:
        raise AmfiError("AMFI's scheme master lists no scheme")
    return records


def _isin(text: str) -> str | None:
    return None if text in ("", "-") else text


def nav_windows(start: date, end: date) -> list[tuple[date, date]]:
    """``start`` to ``end``, inclusive, in consecutive windows of at most four years."""
    windows = []
    first = start
    while first <= end:
        after = _years_later(first, WINDOW_YEARS)
        last = min(end, after - timedelta(days=1))
        windows.append((first, last))
        first = last + timedelta(days=1)
    return windows


def _years_later(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def nav_history_url(scheme_code: int, start: date, end: date) -> str:
    query = urlencode(
        {
            "query_type": "historical_period",
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "sd_id": scheme_code,
        }
    )
    return f"{NAV_HISTORY_URL}?{query}"


def fetch_nav_history(
    session: AmfiSession, scheme_code: int, start: date, end: date
) -> list[NavRecord]:
    """Every NAV AMFI publishes for ``scheme_code`` from ``start`` to ``end``, a window a call."""
    records: list[NavRecord] = []
    for first, last in nav_windows(start, end):
        url = nav_history_url(scheme_code, first, last)
        response = session.get(url)
        if response.status != 200:
            raise AmfiError(f"AMFI's NAV history answered HTTP {response.status} for {url}")
        records.extend(parse_nav_history(scheme_code, response.body))
    return records


def parse_nav_history(scheme_code: int, body: bytes) -> list[NavRecord]:
    try:
        document = json.loads(body, parse_float=Decimal)
        groups = document["data"]["nav_groups"]
    except (ValueError, KeyError, TypeError) as error:
        raise AmfiError(f"scheme {scheme_code}: not an AMFI NAV history: {error}") from error
    records = []
    for group in groups:
        for record in group["historical_records"]:
            records.append(
                NavRecord(
                    scheme_code=scheme_code,
                    nav_date=date.fromisoformat(record["date"]),
                    nav=Decimal(str(record["nav"])),
                    uploaded_at=_uploaded_at(record.get("upload_time")),
                )
            )
    return records


def _uploaded_at(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.removesuffix("Z")).replace(tzinfo=None)
