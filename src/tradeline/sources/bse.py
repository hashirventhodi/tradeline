"""BSE: corporate announcements.

BSE's announcements page reads a JSON API on ``api.bseindia.com``, 50 filings
a page, newest first, with the day's total in ``Table1[0].ROWCNT``. The API
answers only a browser: it wants the site as Referer and Origin and refuses
any client whose TLS handshake is not a browser's (live use needs
``LiveTransport(browser_tls=True)``). ``BseSession`` owns the headers and the
backoff, as ``NseSession`` does for NSE.

A filing names its company by BSE scrip code and BSE's company name; it
carries no NSE symbol and no ISIN. BSE states when it received the filing
(``News_submission_dt``) and when it disseminated it (``DissemDT``), both IST.
The attachment lives under one of two folders, chosen by ``PDFFLAG``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from tradeline.transport import Request, Response, Transport

log = logging.getLogger(__name__)

SITE_HOST = "https://www.bseindia.com"
API_HOST = "https://api.bseindia.com"
ANNOUNCEMENTS_URL = f"{API_HOST}/BseIndiaAPI/api/AnnSubCategoryGetData/w"
# PDFFLAG 0: a recent filing; 1: one moved to the historical folder.
ATTACHMENT_FOLDERS = {
    0: f"{SITE_HOST}/xml-data/corpfiling/AttachLive/",
    1: f"{SITE_HOST}/xml-data/corpfiling/AttachHis/",
}
IST = ZoneInfo("Asia/Kolkata")

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE_HOST}/",
    "Origin": SITE_HOST,
}

RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 3.0, 9.0)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# A page beyond this means the day's count is not being reached; stop rather than loop.
MAX_PAGES = 200


class BseError(RuntimeError):
    pass


class BseSession:
    """Browser headers and backoff for every BSE call."""

    def __init__(self, transport: Transport, *, sleep: Callable[[float], None] = time.sleep):
        self._transport = transport
        self._sleep = sleep

    def get(self, url: str) -> Response:
        """GET ``url`` as the BSE site would. Non-2xx responses are returned, not raised."""
        request = Request("GET", url, headers=BROWSER_HEADERS)
        response = self._transport.send(request)
        for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
            if response.status not in _RETRY_STATUSES:
                break
            log.warning(
                "bse throttled or failing; backing off",
                extra={"url": url, "status": response.status, "attempt": attempt, "delay": delay},
            )
            self._sleep(delay)
            response = self._transport.send(request)
        return response


@dataclass(frozen=True)
class BseAnnouncement:
    """One corporate announcement as BSE lists it."""

    news_id: str
    scrip_code: str
    company_name: str | None
    received_at: datetime | None
    # The official timestamp: when BSE made the filing public.
    disseminated_at: datetime
    subject: str
    body: str | None
    attachment_url: str | None


def announcements_url(day: date, page: int) -> str:
    params = {
        "pageno": str(page),
        "strCat": "-1",
        "strPrevDate": f"{day:%Y%m%d}",
        "strScrip": "",
        "strSearch": "P",
        "strToDate": f"{day:%Y%m%d}",
        "strType": "C",
        "subcategory": "-1",
    }
    return f"{ANNOUNCEMENTS_URL}?{urlencode(params)}"


@dataclass(frozen=True)
class AnnouncementsFetch:
    announcements: list[BseAnnouncement]
    requests: int


def fetch_announcements(session: BseSession, day: date) -> AnnouncementsFetch:
    """Every announcement BSE disseminated on ``day`` (IST), page by page."""
    announcements: list[BseAnnouncement] = []
    page = 0
    while page < MAX_PAGES:
        page += 1
        url = announcements_url(day, page)
        response = session.get(url)
        if response.status != 200:
            raise BseError(f"HTTP {response.status} fetching {url}")
        rows, total = parse_announcements_page(response.body)
        announcements.extend(rows)
        if not rows or len(announcements) >= total:
            return AnnouncementsFetch(announcements, page)
    raise BseError(f"announcements for {day} did not end within {MAX_PAGES} pages")


def parse_announcements_page(body: bytes) -> tuple[list[BseAnnouncement], int]:
    """A page's filings and the day's total count."""
    try:
        document = json.loads(body)
    except ValueError as error:
        raise BseError("announcements response is not JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("Table"), list):
        raise BseError(f"announcements response has no Table: {str(document)[:80]}")
    counts = document.get("Table1") or [{}]
    total = int(counts[0].get("ROWCNT") or 0)
    return [_announcement(record) for record in document["Table"]], total


def _announcement(record: dict[str, Any]) -> BseAnnouncement:
    attachment = _text(record.get("ATTACHMENTNAME"))
    folder = ATTACHMENT_FOLDERS.get(record.get("PDFFLAG"))  # type: ignore[arg-type]
    # BSE's own notices (a clarification it sought, say) have no submission time.
    received = _text(record.get("News_submission_dt"))
    return BseAnnouncement(
        news_id=_required(record, "NEWSID"),
        scrip_code=_required(record, "SCRIP_CD"),
        company_name=_text(record.get("SLONGNAME")),
        received_at=None if received is None else _bse_time(received),
        disseminated_at=_bse_time(_required(record, "DissemDT")),
        subject=" ".join(_required(record, "NEWSSUB").split()),
        body=_text(record.get("HEADLINE")),
        attachment_url=None if attachment is None or folder is None else folder + attachment,
    )


def _bse_time(text: str) -> datetime:
    """BSE's ``2024-07-18T23:45:04.897``, in IST."""
    return datetime.fromisoformat(text).replace(tzinfo=IST)


def _required(record: dict[str, Any], key: str) -> str:
    text = _text(record.get(key))
    if text is None:
        raise BseError(f"BSE record has no {key}: {str(record)[:120]}")
    return text


def _text(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None
