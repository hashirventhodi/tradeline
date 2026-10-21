"""``ingest announcements`` against recorded NSE and BSE fixtures for 23 July 2024.

The instrument master comes from ``ingest eod`` over the same day
(``fixtures/ingest_announcements_master``), trimmed to seven symbols: the five
of the other end-of-day fixtures plus Jain Irrigation's ordinary and DVR
shares, JISLJALEQS and JISLDVREQS, which NSE's master names identically
("Jain Irrigation Systems Limited").

NSE lists eight filings that day in the fixture: INFY (2), RELIANCE (2),
BAJAJ-AUTO, JISLJALEQS, JISLDVREQS, and AJRINFRA (not in the trimmed master).
BSE lists nine over two pages: Infosys Ltd (2), Reliance Industries Ltd,
Bajaj Auto Ltd, Jain Irrigation Systems Ltd (the ambiguous name), Jain
Irrigation Systems Ltd_DVR, two with a blank company name, and an exchange
notice to Oceanic Foods Ltd that has no submission time.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest

from tradeline import queries
from tradeline.dates import IST
from tradeline.db import app_connection
from tradeline.ingest.announcements import AnnouncementIngestResult, ingest_announcements
from tradeline.ingest.eod import ingest_eod
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures" / "ingest_announcements"
MASTER_FIXTURES = Path(__file__).parent / "fixtures" / "ingest_announcements_master"
DAY = date(2024, 7, 23)
LATER = datetime(2026, 1, 1, tzinfo=IST)
NSE_FILINGS, BSE_FILINGS = 8, 9


def ist(hour: int, minute: int, second: int, microsecond: int = 0) -> datetime:
    return datetime(2024, 7, 23, hour, minute, second, microsecond, tzinfo=IST)


@dataclass(frozen=True)
class TwoRuns:
    first: AnnouncementIngestResult
    second: AnnouncementIngestResult
    bse_calls: int


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> TwoRuns:
    """The day's master, then the day's announcements ingested twice."""
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(MASTER_FIXTURES), start=DAY, end=DAY)
        playback = PlaybackTransport(FIXTURES)
        first = ingest_announcements(conn, playback, start=DAY, end=DAY)
        bse_calls = sum(1 for r in playback.calls if "bseindia" in r.url)
        second = ingest_announcements(conn, PlaybackTransport(FIXTURES), start=DAY, end=DAY)
    return TwoRuns(first, second, bse_calls)


@pytest.fixture
def conn(database_url: str, two_runs: TwoRuns) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_a_sample_day_loads_both_exchanges_with_their_official_timestamps(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    infy = queries.announcements(conn, "INFY", start=DAY, end=DAY, as_of=LATER)

    assert [(a.source, a.disseminated_at) for a in infy] == [
        ("bse", ist(18, 23, 35, 580_000)),
        ("nse", ist(18, 24, 9)),
        ("nse", ist(18, 36, 49)),
        ("bse", ist(18, 40, 7, 860_000)),
    ]
    nse_acquisition, bse_acquisition = infy[2], infy[3]
    assert (nse_acquisition.source_id, nse_acquisition.exchange_code) == ("105923264", "INFY")
    assert (nse_acquisition.company_name, nse_acquisition.subject) == (
        "Infosys Limited",
        "Acquisition",
    )
    assert nse_acquisition.received_at == ist(18, 36, 48)
    assert nse_acquisition.body is not None
    assert nse_acquisition.body.startswith("Infosys Limited has informed the Exchange")
    assert nse_acquisition.attachment_url == (
        "https://nsearchives.nseindia.com/corporate/INFY_23072024183648_ExchangeletterJuly232024.pdf"
    )
    assert (bse_acquisition.exchange_code, bse_acquisition.company_name) == (
        "500209",
        "Infosys Ltd",
    )
    assert bse_acquisition.received_at == ist(18, 40, 7)
    assert bse_acquisition.attachment_url is not None
    assert bse_acquisition.attachment_url.startswith(
        "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
    )
    # BSE's "Bajaj Auto Ltd" is NSE's "Bajaj Auto Limited".
    bajaj = queries.announcements(conn, "BAJAJ-AUTO", start=DAY, end=DAY, as_of=LATER)
    assert [(a.source, a.disseminated_at) for a in bajaj] == [
        ("bse", ist(15, 7, 34, 520_000)),
        ("nse", ist(15, 9, 34)),
    ]


def test_each_exchange_is_recorded_as_its_own_run_with_match_counts(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    nse_run = queries.ingest_runs(conn, source="nse_announcements")[-1]  # oldest
    bse_run = queries.ingest_runs(conn, source="bse_announcements")[-1]

    assert (nse_run.command, nse_run.range_start, nse_run.range_end) == ("ingest", DAY, DAY)
    assert (nse_run.rows_seen, nse_run.rows_written, nse_run.error) == (8, 8, None)
    assert nse_run.details == {"requests": 1, "matched": 7, "unmatched": 1}
    assert (bse_run.rows_seen, bse_run.rows_written, bse_run.error) == (9, 9, None)
    assert bse_run.details == {"requests": 2, "matched": 4, "unmatched": 5}
    assert two_runs.bse_calls == 2  # BSE's two pages, read until the day's count is reached


def test_a_bse_filing_under_a_name_two_symbols_share_is_stored_unmatched(
    conn: psycopg.Connection,
) -> None:
    unmatched = queries.announcements(conn, None, start=DAY, end=DAY, as_of=LATER)
    jain_ordinary = queries.announcements(conn, "JISLJALEQS", start=DAY, end=DAY, as_of=LATER)
    jain_dvr = queries.announcements(conn, "JISLDVREQS", start=DAY, end=DAY, as_of=LATER)

    ambiguous = [a for a in unmatched if a.company_name == "Jain Irrigation Systems Ltd"]
    assert [(a.source, a.exchange_code, a.symbol) for a in ambiguous] == [("bse", "500219", None)]
    # NSE's own filings for the two share classes carry their symbols, so they match exactly.
    assert [(a.source, a.exchange_code) for a in jain_ordinary] == [("nse", "JISLJALEQS")]
    assert [(a.source, a.exchange_code) for a in jain_dvr] == [("nse", "JISLDVREQS")]
    assert sorted((a.source, a.exchange_code) for a in unmatched) == [
        ("bse", "132541"),  # blank company name, twice
        ("bse", "132541"),
        ("bse", "500219"),  # Jain Irrigation Systems Ltd: two symbols
        ("bse", "540405"),  # Oceanic Foods Ltd: not in the trimmed master
        ("bse", "570004"),  # Jain Irrigation Systems Ltd_DVR: no symbol has this name
        ("nse", "AJRINFRA"),  # not in the trimmed master
    ]


def test_a_filing_with_no_submission_time_or_company_name_is_still_stored(
    conn: psycopg.Connection,
) -> None:
    unmatched = queries.announcements(conn, None, start=DAY, end=DAY, as_of=LATER)
    by_code = {(a.source, a.exchange_code): a for a in unmatched}

    notice = by_code[("bse", "540405")]
    assert (notice.company_name, notice.received_at) == ("Oceanic Foods Ltd", None)
    assert notice.disseminated_at == ist(19, 9, 1, 197_000)
    assert by_code[("bse", "132541")].company_name is None


def test_rerunning_the_same_day_inserts_nothing(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    newest_nse = queries.ingest_runs(conn, source="nse_announcements")[0]
    newest_bse = queries.ingest_runs(conn, source="bse_announcements")[0]

    assert (two_runs.second.nse.rows_written, two_runs.second.bse.rows_written) == (0, 0)
    assert (newest_nse.rows_seen, newest_nse.rows_written) == (NSE_FILINGS, 0)
    assert (newest_bse.rows_seen, newest_bse.rows_written) == (BSE_FILINGS, 0)
    assert len(queries.announcements(conn, "INFY", start=DAY, end=DAY, as_of=LATER)) == 4


def test_an_announcement_disseminated_after_the_as_of_time_is_excluded(
    conn: psycopg.Connection,
) -> None:
    at_half_past_six = queries.announcements(conn, "INFY", start=DAY, end=DAY, as_of=ist(18, 30, 0))
    at_the_moment_of_dissemination = queries.announcements(
        conn, "INFY", start=DAY, end=DAY, as_of=ist(18, 36, 49)
    )

    assert [a.disseminated_at for a in at_half_past_six] == [
        ist(18, 23, 35, 580_000),
        ist(18, 24, 9),
    ]
    assert [a.disseminated_at for a in at_the_moment_of_dissemination][-1] == ist(18, 36, 49)


def test_the_range_is_indian_calendar_days_of_the_official_timestamp(
    conn: psycopg.Connection,
) -> None:
    """A filing at 00:20 IST on the 23rd is 18:50 UTC on the 22nd, and belongs to the 23rd."""
    on_the_day = queries.announcements(conn, None, start=DAY, end=DAY, as_of=LATER)
    day_before = queries.announcements(
        conn, None, start=date(2024, 7, 22), end=date(2024, 7, 22), as_of=LATER
    )

    assert on_the_day[0].disseminated_at == ist(0, 20, 42, 863_000)
    assert day_before == []


def test_a_naive_as_of_time_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        queries.announcements(conn, "INFY", start=DAY, end=DAY, as_of=datetime(2024, 7, 23, 18))
