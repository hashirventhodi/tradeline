"""NSE: end-of-day files, the equity master, index constituents, corporate actions, and
announcements.

NSE's site refuses requests that do not look like a browser and rate-limits
aggressively. ``NseSession`` owns that: it sends browser headers on every
request, keeps the session cookies the site sets, warms the session up before
calling the JSON API on ``www.nseindia.com``, and backs off on throttling.
Adapters call ``session.get`` and never touch headers themselves.

Index constituent lists are CSV files on the archives host, one row per
constituent with NSE's sector in a column headed ``Industry``.

Corporate actions come from the site's JSON API, filtered by ex-date range and
optionally by symbol. Each record is a free-text ``subject`` ("Bonus 1:1",
"Dividend - Rs 10 Per Share"); reading it is ``tradeline.adjust``'s job.

Announcements come from the same API, one calendar day per request. NSE
states two times for each: when the exchange received the filing (``an_dt``)
and when it disseminated it to the public (``exchdisstime``), both in IST.

End-of-day files come in two layouts. Up to 2024-07-05 NSE published the
legacy ``cmDDMMMYYYYbhav.csv.zip``; from 2024-07-08 only the UDiFF
``BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`` exists (UDiFF is not
available before January 2024). The URL is chosen by date and the layout is
detected from the header, so a file served at either URL parses correctly.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

from tradeline.dates import IST
from tradeline.transport import Request, Response, Transport

log = logging.getLogger(__name__)

ARCHIVES_HOST = "https://nsearchives.nseindia.com"
SITE_HOST = "https://www.nseindia.com"
EQUITY_MASTER_URL = f"{ARCHIVES_HOST}/content/equities/EQUITY_L.csv"
CORPORATE_ACTIONS_URL = f"{SITE_HOST}/api/corporates-corporateActions"
ANNOUNCEMENTS_URL = f"{SITE_HOST}/api/corporate-announcements"

# The first trading day with no legacy file; UDiFF is used from here on.
UDIFF_FROM = date(2024, 7, 8)

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE_HOST}/",
}

# Backoff on throttling (429) and server errors: the delay before each retry.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 3.0, 9.0)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_SESSION_EXPIRED_STATUSES = frozenset({401, 403})


class NseError(RuntimeError):
    pass


class NseSession:
    """Browser headers, session cookies, and backoff for every NSE call."""

    def __init__(self, transport: Transport, *, sleep: Callable[[float], None] = time.sleep):
        self._transport = transport
        self._sleep = sleep
        self._cookies: dict[str, str] = {}

    def get(self, url: str) -> Response:
        """GET ``url`` as a browser would. Non-2xx responses are returned, not raised."""
        if _needs_session(url) and not self._cookies:
            self._warm_up()
        response = self._send(url)
        if response.status in _SESSION_EXPIRED_STATUSES and _needs_session(url):
            log.info("nse session expired; renewing", extra={"url": url, "status": response.status})
            self._cookies.clear()
            self._warm_up()
            response = self._send(url)
        return response

    def _warm_up(self) -> None:
        self._send(f"{SITE_HOST}/")

    def _send(self, url: str) -> Response:
        headers = dict(BROWSER_HEADERS)
        if self._cookies and _is_nse(url):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        response = self._transport.send(Request("GET", url, headers=headers))
        for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
            if response.status not in _RETRY_STATUSES:
                break
            log.warning(
                "nse throttled or failing; backing off",
                extra={"url": url, "status": response.status, "attempt": attempt, "delay": delay},
            )
            self._sleep(delay)
            response = self._transport.send(Request("GET", url, headers=headers))
        if _is_nse(url):
            self._remember_cookies(response)
        return response

    def _remember_cookies(self, response: Response) -> None:
        for header in response.header_values("set-cookie"):
            pair = header.split(";", 1)[0]
            name, _, value = pair.partition("=")
            if name.strip():
                self._cookies[name.strip()] = value.strip()


def _is_nse(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "nseindia.com" or host.endswith(".nseindia.com")


def _needs_session(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == "www.nseindia.com" and parts.path.startswith("/api/")


# --- End-of-day files -------------------------------------------------------


@dataclass(frozen=True)
class EndOfDayRow:
    trade_date: date
    symbol: str
    series: str
    isin: str | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    last: Decimal | None
    prev_close: Decimal | None
    traded_qty: int
    traded_value: Decimal
    trades: int


def bhavcopy_url(day: date) -> str:
    if day >= UDIFF_FROM:
        return f"{ARCHIVES_HOST}/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    month = f"{day:%b}".upper()  # JAN, FEB, ...
    return (
        f"{ARCHIVES_HOST}/content/historical/EQUITIES/{day:%Y}/{month}/"
        f"cm{day:%d}{month}{day:%Y}bhav.csv.zip"
    )


def fetch_bhavcopy(session: NseSession, day: date) -> list[EndOfDayRow] | None:
    """The day's rows, or None when NSE has no file for that day (a holiday, or a gap)."""
    url = bhavcopy_url(day)
    response = session.get(url)
    if response.status == 404:
        return None
    if response.status != 200:
        raise NseError(f"HTTP {response.status} fetching {url}")
    return parse_bhavcopy(response.body, day)


def parse_bhavcopy(body: bytes, day: date) -> list[EndOfDayRow]:
    text = _unzip_single(body)
    reader = csv.DictReader(io.StringIO(text))
    header = [name.strip() for name in reader.fieldnames or ()]
    if "TckrSymb" in header:
        rows = [_udiff_row(record) for record in reader]
    elif "SYMBOL" in header:
        rows = [_legacy_row(record) for record in reader]
    else:
        raise NseError(f"unrecognised end-of-day file layout for {day}: header {header[:5]}")
    wrong_day = {row.trade_date for row in rows} - {day}
    if wrong_day:
        raise NseError(f"end-of-day file for {day} holds rows dated {sorted(wrong_day)}")
    return rows


def _udiff_row(record: dict[str, str]) -> EndOfDayRow:
    return EndOfDayRow(
        trade_date=date.fromisoformat(record["TradDt"]),
        symbol=record["TckrSymb"].strip(),
        series=record["SctySrs"].strip(),
        isin=_text(record["ISIN"]),
        open=_decimal(record["OpnPric"]),
        high=_decimal(record["HghPric"]),
        low=_decimal(record["LwPric"]),
        close=_decimal(record["ClsPric"]),
        last=_decimal(record["LastPric"]),
        prev_close=_decimal(record["PrvsClsgPric"]),
        traded_qty=_int(record["TtlTradgVol"]),
        traded_value=_decimal(record["TtlTrfVal"]) or Decimal(0),
        trades=_int(record["TtlNbOfTxsExctd"]),
    )


def _legacy_row(record: dict[str, str]) -> EndOfDayRow:
    return EndOfDayRow(
        trade_date=_nse_date(record["TIMESTAMP"]),
        symbol=record["SYMBOL"].strip(),
        series=record["SERIES"].strip(),
        isin=_text(record.get("ISIN")),  # absent before mid-2011
        open=_decimal(record["OPEN"]),
        high=_decimal(record["HIGH"]),
        low=_decimal(record["LOW"]),
        close=_decimal(record["CLOSE"]),
        last=_decimal(record["LAST"]),
        prev_close=_decimal(record["PREVCLOSE"]),
        traded_qty=_int(record["TOTTRDQTY"]),
        traded_value=_decimal(record["TOTTRDVAL"]) or Decimal(0),
        trades=_int(record["TOTALTRADES"]),
    )


# --- Equity master ----------------------------------------------------------


@dataclass(frozen=True)
class ListedSymbol:
    symbol: str
    series: str
    name: str
    isin: str
    listing_date: date


def fetch_equity_master(session: NseSession) -> list[ListedSymbol]:
    response = session.get(EQUITY_MASTER_URL)
    if response.status != 200:
        raise NseError(f"HTTP {response.status} fetching {EQUITY_MASTER_URL}")
    return parse_equity_master(response.body)


def parse_equity_master(body: bytes) -> list[ListedSymbol]:
    # Header names carry leading spaces (" SERIES", " ISIN NUMBER"); strip them.
    reader = csv.reader(io.StringIO(body.decode("utf-8")))
    header = [name.strip() for name in next(reader)]
    rows: list[ListedSymbol] = []
    for values in reader:
        if not values:
            continue
        record = dict(zip(header, (v.strip() for v in values), strict=False))
        rows.append(
            ListedSymbol(
                symbol=record["SYMBOL"],
                series=record["SERIES"],
                name=record["NAME OF COMPANY"],
                isin=record["ISIN NUMBER"],
                listing_date=_nse_date(record["DATE OF LISTING"]),
            )
        )
    return rows


# --- Index constituents -----------------------------------------------------

# The NIFTY Total Market list (about 750 symbols: the NIFTY 500 and the
# Microcap 250) is the widest list NSE publishes with a sector per symbol.
TOTAL_MARKET_LIST = "ind_niftytotalmarket_list"
NIFTY_200_LIST = "ind_nifty200list"


@dataclass(frozen=True)
class IndexConstituent:
    symbol: str
    series: str
    name: str
    isin: str
    # NSE's sector classification; the file heads this column "Industry".
    sector: str


def index_constituents_url(index_list: str) -> str:
    return f"{ARCHIVES_HOST}/content/indices/{index_list}.csv"


def fetch_index_constituents(session: NseSession, index_list: str) -> list[IndexConstituent]:
    url = index_constituents_url(index_list)
    response = session.get(url)
    if response.status != 200:
        raise NseError(f"HTTP {response.status} fetching {url}")
    return parse_index_constituents(response.body)


def parse_index_constituents(body: bytes) -> list[IndexConstituent]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    header = [name.strip() for name in reader.fieldnames or ()]
    expected = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
    if not expected <= set(header):
        raise NseError(f"unrecognised index constituent list: header {header}")
    rows: list[IndexConstituent] = []
    for raw in reader:
        record = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not record["Symbol"]:
            continue
        rows.append(
            IndexConstituent(
                symbol=record["Symbol"],
                series=record["Series"],
                name=record["Company Name"],
                isin=record["ISIN Code"],
                sector=record["Industry"],
            )
        )
    return rows


# --- Corporate actions ------------------------------------------------------


@dataclass(frozen=True)
class CorporateActionNotice:
    """One record of NSE's corporate-action list, before we read its subject."""

    symbol: str
    series: str
    isin: str | None
    ex_date: date
    record_date: date | None
    face_value: Decimal | None
    # NSE's text with its whitespace collapsed; otherwise identical records differ by a space.
    subject: str


def corporate_actions_url(start: date, end: date, *, symbol: str | None = None) -> str:
    params = {"index": "equities", "from_date": f"{start:%d-%m-%Y}", "to_date": f"{end:%d-%m-%Y}"}
    if symbol is not None:
        params["symbol"] = symbol
    return f"{CORPORATE_ACTIONS_URL}?{urlencode(params)}"


def fetch_corporate_actions(
    session: NseSession, *, start: date, end: date, symbol: str | None = None
) -> list[CorporateActionNotice]:
    """Every action with an ex-date from ``start`` to ``end`` inclusive (one symbol's, if named)."""
    url = corporate_actions_url(start, end, symbol=symbol)
    response = session.get(url)
    if response.status != 200:
        raise NseError(f"HTTP {response.status} fetching {url}")
    return parse_corporate_actions(response.body)


def parse_corporate_actions(body: bytes) -> list[CorporateActionNotice]:
    try:
        records = json.loads(body)
    except ValueError as error:
        raise NseError("corporate-actions response is not JSON") from error
    if not isinstance(records, list):
        raise NseError(f"corporate-actions response is not a list: {str(records)[:80]}")
    return [_notice(record) for record in records]


def _notice(record: dict[str, Any]) -> CorporateActionNotice:
    return CorporateActionNotice(
        symbol=_required(record, "symbol"),
        series=_required(record, "series"),
        isin=_text(record.get("isin")),
        ex_date=_nse_date(_required(record, "exDate")),
        record_date=_optional_nse_date(record.get("recDate")),
        face_value=_lenient_decimal(record.get("faceVal")),
        subject=" ".join(_required(record, "subject").split()),
    )


def _required(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    text = _text(None if value is None else str(value))
    if text is None:
        raise NseError(f"NSE record has no {key}: {str(record)[:120]}")
    return text


# --- Announcements ----------------------------------------------------------


@dataclass(frozen=True)
class NseAnnouncement:
    """One corporate announcement as NSE lists it."""

    seq_id: str
    symbol: str
    company_name: str | None
    isin: str | None
    received_at: datetime
    # The official timestamp: when NSE made the filing public. NSE's received
    # time stands in where it gives no dissemination time.
    disseminated_at: datetime
    subject: str
    body: str | None
    attachment_url: str | None


def announcements_url(day: date) -> str:
    params = {"index": "equities", "from_date": f"{day:%d-%m-%Y}", "to_date": f"{day:%d-%m-%Y}"}
    return f"{ANNOUNCEMENTS_URL}?{urlencode(params)}"


def fetch_announcements(session: NseSession, day: date) -> list[NseAnnouncement]:
    """Every announcement NSE received on ``day`` (IST)."""
    url = announcements_url(day)
    response = session.get(url)
    if response.status != 200:
        raise NseError(f"HTTP {response.status} fetching {url}")
    return parse_announcements(response.body)


def parse_announcements(body: bytes) -> list[NseAnnouncement]:
    try:
        records = json.loads(body)
    except ValueError as error:
        raise NseError("announcements response is not JSON") from error
    if not isinstance(records, list):
        raise NseError(f"announcements response is not a list: {str(records)[:80]}")
    return [_announcement(record) for record in records]


def _announcement(record: dict[str, Any]) -> NseAnnouncement:
    received_at = _nse_time(_required(record, "an_dt"))
    disseminated = _text(_string(record.get("exchdisstime")))
    return NseAnnouncement(
        seq_id=_required(record, "seq_id"),
        symbol=_required(record, "symbol"),
        company_name=_text(_string(record.get("sm_name"))),
        isin=_text(_string(record.get("sm_isin"))),
        received_at=received_at,
        disseminated_at=received_at if disseminated is None else _nse_time(disseminated),
        subject=" ".join(_required(record, "desc").split()),
        body=_text(_string(record.get("attchmntText"))),
        attachment_url=_optional_text(record.get("attchmntFile")),
    )


def _optional_text(value: Any) -> str | None:
    """NSE's text, or None where it writes ``-`` (or nothing)."""
    text = _text(_string(value))
    return None if text == "-" else text


def _nse_time(text: str) -> datetime:
    """NSE's ``18-Jul-2024 23:48:42``, in IST."""
    return datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


# --- Helpers ----------------------------------------------------------------


def _unzip_single(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise NseError(f"expected one file in the archive, found {names}")
            return archive.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as error:
        raise NseError("response is not a zip archive") from error


def _nse_date(text: str) -> date:
    """NSE's ``04-JAN-2019`` form."""
    return datetime.strptime(text.strip(), "%d-%b-%Y").date()


def _optional_nse_date(value: Any) -> date | None:
    """An NSE date, or None where NSE writes ``-`` (or nothing) for a date it has not set."""
    text = _text(None if value is None else str(value))
    return None if text is None or text == "-" else _nse_date(text)


def _lenient_decimal(value: Any) -> Decimal | None:
    """A number where NSE gives one, None where it gives ``-``, blank, or anything else."""
    text = _text(None if value is None else str(value))
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _decimal(value: str | None) -> Decimal | None:
    value = (value or "").strip()
    return Decimal(value) if value else None


def _int(value: str | None) -> int:
    value = (value or "").strip()
    return int(Decimal(value)) if value else 0
