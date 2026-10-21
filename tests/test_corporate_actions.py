"""``ingest corporate-actions`` against the recorded NSE fixture, and the adjusted series it yields.

The fixture is NSE's corporate-action list for ex-dates 2024-07-01 to
2024-12-31, trimmed to the five symbols of the end-of-day fixtures plus rows
that exercise the reading of subjects: a bonus and a split on the same day
(VERTOZ), a split in NSE's "Rs10/-" spelling (KPIGREEN), a rights issue
(JAIPURKURT), a demerger (OCCL), and a record NSE lists twice (NORBTEAEXP).

The adjusted series is checked two ways: by hand against the raw end-of-day
rows, and against Kite's own adjusted RELIANCE bars in the candle fixtures,
which were fetched after RELIANCE's 1:1 bonus of 2024-10-28.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import (
    CorporateActionIngestResult,
    ingest_corporate_actions,
    year_chunks,
)
from tradeline.ingest.eod import ingest_eod
from tradeline.sources.kite import KiteSession
from tradeline.sources.nse import NseError
from tradeline.transport import PlaybackTransport, Request, Response

FIXTURES = Path(__file__).parent / "fixtures" / "ingest_corporate_actions"
EOD_FIXTURES = Path(__file__).parent / "fixtures" / "ingest_eod"
CANDLE_FIXTURES = Path(__file__).parent / "fixtures" / "ingest_candles"
START, END = date(2024, 7, 1), date(2024, 8, 30)  # the end-of-day and candle fixtures
ACTIONS_START, ACTIONS_END = date(2024, 7, 1), date(2024, 12, 31)
TRADING_DAYS = 43
RECORDS, UNIQUE_RECORDS = 20, 19
BONUS_EX_DATE = date(2024, 10, 28)  # RELIANCE 1:1
DIVIDEND_EX_DATE = date(2024, 8, 19)  # RELIANCE Rs 10
D = Decimal


@dataclass(frozen=True)
class TwoRuns:
    first: CorporateActionIngestResult
    second: CorporateActionIngestResult


@pytest.fixture(scope="module")
def two_runs(database_url: str) -> TwoRuns:
    """The master and end-of-day rows, Kite's RELIANCE bars, then the actions ingested twice."""
    with app_connection(database_url) as conn:
        ingest_eod(conn, PlaybackTransport(EOD_FIXTURES), start=START, end=END)
        session = KiteSession(
            PlaybackTransport(CANDLE_FIXTURES),
            api_key="key",
            access_token="tok",
            sleep=lambda _: None,
        )
        ingest_candles(conn, session, start=START, end=END, symbols=["RELIANCE"])
        first = ingest_corporate_actions(
            conn, PlaybackTransport(FIXTURES), start=ACTIONS_START, end=ACTIONS_END
        )
        second = ingest_corporate_actions(
            conn, PlaybackTransport(FIXTURES), start=ACTIONS_START, end=ACTIONS_END
        )
    return TwoRuns(first, second)


@pytest.fixture
def conn(database_url: str, two_runs: TwoRuns) -> Iterator[psycopg.Connection]:
    with app_connection(database_url) as conn:
        yield conn


def test_a_fixture_backed_run_stores_every_action_with_its_kind_and_factor(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    run = queries.ingest_runs(conn, source="nse_corporate_actions")[-1]  # oldest

    assert (two_runs.first.rows_seen, two_runs.first.rows_written) == (RECORDS, UNIQUE_RECORDS)
    assert (two_runs.first.requests, two_runs.first.unparsed, two_runs.first.anomalies) == (1, 0, 0)
    assert run.command == "ingest"
    assert (run.range_start, run.range_end) == (ACTIONS_START, ACTIONS_END)
    assert (run.rows_seen, run.rows_written, run.error) == (RECORDS, UNIQUE_RECORDS, None)
    assert run.details["by_kind"] == {
        "bonus": 2,
        "dividend": 8,
        "other": 7,
        "rights": 1,
        "split": 2,
    }

    dividend, bonus = queries.corporate_actions(conn, "RELIANCE")
    assert (dividend.ex_date, dividend.kind, dividend.factor) == (
        DIVIDEND_EX_DATE,
        "dividend",
        None,
    )
    assert dividend.subject == "Dividend - Rs 10 Per Share"
    assert (bonus.ex_date, bonus.kind, bonus.factor) == (BONUS_EX_DATE, "bonus", Fraction(1, 2))
    assert (bonus.series, bonus.isin, bonus.record_date, bonus.face_value) == (
        "EQ",
        "INE002A01018",
        BONUS_EX_DATE,
        D("10"),
    )


def test_two_actions_on_one_day_a_split_a_rights_issue_and_a_demerger_are_each_read(
    conn: psycopg.Connection,
) -> None:
    vertoz_bonus, vertoz_split = queries.corporate_actions(conn, "VERTOZ")
    (kpigreen_split,) = queries.corporate_actions(conn, "KPIGREEN", end=date(2024, 7, 31))
    (rights, meeting) = queries.corporate_actions(conn, "JAIPURKURT")
    demerger, agm_dividend = queries.corporate_actions(conn, "OCCL")

    assert (vertoz_bonus.ex_date, vertoz_bonus.kind, vertoz_bonus.factor) == (
        date(2024, 7, 5),
        "bonus",
        Fraction(1, 2),
    )
    assert (vertoz_split.ex_date, vertoz_split.kind, vertoz_split.factor) == (
        date(2024, 7, 5),
        "split",
        Fraction(1, 10),
    )
    assert (kpigreen_split.kind, kpigreen_split.factor) == ("split", Fraction(1, 2))
    assert (rights.kind, rights.factor, rights.subject) == (
        "rights",
        None,
        "Rights 100:224 @ Premium Rs 20/-",
    )
    assert (meeting.kind, meeting.factor) == ("other", None)
    assert (demerger.kind, demerger.subject) == ("other", "Demerger")
    assert (agm_dividend.kind, agm_dividend.record_date) == ("dividend", None)


def test_a_second_run_over_the_same_range_writes_nothing(
    conn: psycopg.Connection, two_runs: TwoRuns
) -> None:
    newest = queries.ingest_runs(conn, source="nse_corporate_actions")[0]

    assert two_runs.second.rows_written == 0
    assert (newest.rows_seen, newest.rows_written) == (RECORDS, 0)


def test_a_record_nse_lists_twice_is_stored_once_keeping_the_first(
    conn: psycopg.Connection,
) -> None:
    """NSE lists NORBTEAEXP's meeting of 2024-12-23 twice, the second time without a record date."""
    (_agm, meeting) = queries.corporate_actions(conn, "NORBTEAEXP")

    assert (meeting.ex_date, meeting.record_date) == (date(2024, 12, 23), date(2024, 12, 23))


def test_the_adjusted_series_matches_hand_computed_values_to_the_paisa(
    conn: psycopg.Connection,
) -> None:
    """RELIANCE's raw bar of 1 July 2024: 3125.05 / 3158.8 / 3111.35 / 3120.3, 3,862,153 shares."""
    series = queries.adjusted_daily_candles(
        conn, "RELIANCE", start=START, end=END, as_of=BONUS_EX_DATE
    )
    first, last = series[0], series[-1]

    assert len(series) == TRADING_DAYS
    assert (first.trade_date, first.symbol, first.series) == (START, "RELIANCE", "EQ")
    assert (first.open, first.high, first.low, first.close) == (
        D("1562.50"),  # 1562.525 is a tie between ticks; the even tick wins
        D("1579.40"),
        D("1555.70"),  # 1555.675, likewise
        D("1560.15"),
    )
    assert (first.volume, first.factor) == (7_724_306, Fraction(1, 2))
    # 30 August 2024: 3068.00 / 3079.45 / 3006.65 / 3019.25 on 20,810,365 shares.
    assert (last.open, last.high, last.low, last.close) == (
        D("1534.00"),
        D("1539.70"),
        D("1503.30"),
        D("1509.60"),
    )
    assert last.volume == 41_620_730


def test_the_adjusted_series_equals_kites_bonus_adjusted_bars_for_every_day(
    conn: psycopg.Connection,
) -> None:
    """Kite fetched RELIANCE after the bonus; its bars are the broker's displayed adjusted chart."""
    kite = queries.daily_candles(conn, "RELIANCE", start=START, end=END)
    ours = queries.adjusted_daily_candles(conn, "RELIANCE", start=START, end=END, as_of=ACTIONS_END)

    assert len(kite) == len(ours) == TRADING_DAYS
    assert [(b.trade_date, b.open, b.high, b.low, b.close, b.volume) for b in ours] == [
        (c.trade_date, c.open, c.high, c.low, c.close, c.volume) for c in kite
    ]


def test_raw_end_of_day_rows_are_unchanged_after_the_adjusted_series_is_read(
    conn: psycopg.Connection,
) -> None:
    before = queries.end_of_day(conn, "RELIANCE", start=START, end=END)
    adjusted = queries.adjusted_daily_candles(
        conn, "RELIANCE", start=START, end=END, as_of=ACTIONS_END
    )
    after = queries.end_of_day(conn, "RELIANCE", start=START, end=END)

    assert before == after
    assert (before[0].close, before[0].traded_qty) == (D("3120.3"), 3_862_153)
    assert (adjusted[0].close, adjusted[0].volume) == (D("1560.15"), 7_724_306)


def test_an_action_dated_after_the_as_of_date_is_not_applied(conn: psycopg.Connection) -> None:
    day_before = queries.adjusted_daily_candles(
        conn, "RELIANCE", start=START, end=END, as_of=date(2024, 10, 27)
    )
    raw = queries.end_of_day(conn, "RELIANCE", start=START, end=END)

    assert all(bar.factor == 1 for bar in day_before)
    assert [(b.open, b.high, b.low, b.close, b.volume) for b in day_before] == [
        (r.open, r.high, r.low, r.close, r.traded_qty) for r in raw
    ]


def test_a_dividend_is_flagged_on_the_bars_before_its_ex_date_once_it_is_known(
    conn: psycopg.Connection,
) -> None:
    known = queries.adjusted_daily_candles(
        conn, "RELIANCE", start=date(2024, 8, 16), end=DIVIDEND_EX_DATE, as_of=DIVIDEND_EX_DATE
    )
    not_yet = queries.adjusted_daily_candles(
        conn, "RELIANCE", start=date(2024, 8, 16), end=DIVIDEND_EX_DATE, as_of=date(2024, 8, 18)
    )
    friday, monday = known

    assert (friday.trade_date, monday.trade_date) == (date(2024, 8, 16), DIVIDEND_EX_DATE)
    assert [a.subject for a in friday.unadjusted] == ["Dividend - Rs 10 Per Share"]
    assert (friday.close, friday.factor) == (D("2956.40"), Fraction(1))  # flagged, not adjusted
    assert monday.unadjusted == ()
    assert all(bar.unadjusted == () for bar in not_yet)


def test_a_symbol_with_only_a_meeting_is_returned_unadjusted_and_unflagged_per_series(
    conn: psycopg.Connection,
) -> None:
    be = queries.adjusted_daily_candles(
        conn, "BAFNAPH", start=START, end=END, as_of=ACTIONS_END, series="BE"
    )
    eq = queries.adjusted_daily_candles(
        conn, "BAFNAPH", start=START, end=END, as_of=ACTIONS_END, series="EQ"
    )

    assert len(be) == TRADING_DAYS
    assert all(bar.factor == 1 and bar.unadjusted == () for bar in be)
    assert eq == []


# --- Behaviour the recorded fixture cannot show, against a stub NSE ---------


class StubNse:
    """Refuses the warm-up as NSE's edge does, and answers every API call with the same body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.api_calls: list[Request] = []

    def send(self, request: Request) -> Response:
        if "/api/" not in request.url:
            return Response(403, (), b"")
        self.api_calls.append(request)
        return Response(200, (("content-type", "application/json"),), self.body)


def record(symbol: str, subject: str, ex_date: str = "05-Jul-2024") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "series": "EQ",
        "isin": "INE000000000",
        "exDate": ex_date,
        "recDate": "-",
        "faceVal": "10",
        "subject": subject,
        "comp": "Stub Limited",
        "bcStartDate": "-",
        "bcEndDate": "-",
        "ndStartDate": "-",
        "ndEndDate": "-",
        "caBroadcastDate": None,
        "ind": "-",
    }


def test_year_chunks_cut_a_range_at_calendar_years_inclusive_at_both_ends() -> None:
    assert list(year_chunks(date(2023, 6, 1), date(2025, 2, 1))) == [
        (date(2023, 6, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 2, 1)),
    ]
    assert list(year_chunks(date(2024, 7, 1), date(2024, 7, 1))) == [
        (date(2024, 7, 1), date(2024, 7, 1))
    ]


def test_a_multi_year_range_is_one_request_per_year_and_per_named_symbol(
    database_url: str, two_runs: TwoRuns
) -> None:
    nse = StubNse(b"[]")
    with app_connection(database_url) as conn:
        result = ingest_corporate_actions(
            conn, nse, start=date(2023, 6, 1), end=date(2025, 2, 1), symbols=["INFY", "BAJAJ-AUTO"]
        )

    assert (result.requests, result.rows_seen, result.rows_written) == (6, 0, 0)
    assert [call.url.split("?")[1] for call in nse.api_calls] == [
        "index=equities&from_date=01-06-2023&to_date=31-12-2023&symbol=INFY",
        "index=equities&from_date=01-06-2023&to_date=31-12-2023&symbol=BAJAJ-AUTO",
        "index=equities&from_date=01-01-2024&to_date=31-12-2024&symbol=INFY",
        "index=equities&from_date=01-01-2024&to_date=31-12-2024&symbol=BAJAJ-AUTO",
        "index=equities&from_date=01-01-2025&to_date=01-02-2025&symbol=INFY",
        "index=equities&from_date=01-01-2025&to_date=01-02-2025&symbol=BAJAJ-AUTO",
    ]


def test_a_subject_that_looks_adjusting_but_yields_no_factor_is_stored_unadjusted_and_reported(
    database_url: str, two_runs: TwoRuns
) -> None:
    nse = StubNse(json.dumps([record("ODD", "Bonus Issue 1:1")]).encode())
    day = date(2024, 7, 5)
    with app_connection(database_url) as conn:
        result = ingest_corporate_actions(conn, nse, start=day, end=day)
        (stored,) = queries.corporate_actions(conn, "ODD")
        run = queries.ingest_runs(conn, source="nse_corporate_actions")[0]

    assert (result.rows_written, result.unparsed) == (1, 1)
    assert (stored.kind, stored.factor) == ("other", None)
    assert run.details["unparsed_subjects"] == [
        {"symbol": "ODD", "ex_date": "2024-07-05", "subject": "Bonus Issue 1:1"}
    ]


def test_a_stored_action_derived_differently_by_a_later_run_is_an_anomaly_and_kept(
    database_url: str, two_runs: TwoRuns
) -> None:
    """As after a parser fix: the row an earlier version stored as 'other' is not rewritten."""
    day = date(2024, 7, 5)
    nse = StubNse(json.dumps([record("REDO", "Bonus 1:1")]).encode())
    with app_connection(database_url) as conn:
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind)"
            " VALUES ('REDO', 'EQ', %s, 'Bonus 1:1', 'other')",
            (day,),
        )
        conn.commit()
        result = ingest_corporate_actions(conn, nse, start=day, end=day)
        (stored,) = queries.corporate_actions(conn, "REDO")
        run = queries.ingest_runs(conn, source="nse_corporate_actions")[0]

    assert (result.rows_written, result.anomalies) == (0, 1)
    assert (stored.kind, stored.factor) == ("other", None)
    assert run.details["anomaly_actions"] == [
        {
            "symbol": "REDO",
            "series": "EQ",
            "ex_date": "2024-07-05",
            "subject": "Bonus 1:1",
            "stored": {"kind": "other", "factor": None},
            "derived": {"kind": "bonus", "factor": "1/2"},
        }
    ]


def test_a_bonus_and_a_split_on_one_day_compound_through_the_query_layer_once_each(
    database_url: str, two_runs: TwoRuns
) -> None:
    """VERTOZ has no end-of-day rows in the fixtures, so two are supplied, with the bonus
    listed a second time under series BE as NSE does for a symbol trading in both."""
    before, ex_date = date(2024, 7, 4), date(2024, 7, 5)
    with app_connection(database_url) as conn:
        conn.execute(
            "INSERT INTO eod_bhavcopy (trade_date, symbol, series, open, high, low, close,"
            " traded_qty, traded_value, trades) VALUES"
            " (%s, 'VERTOZ', 'EQ', 1000.00, 1000.00, 1000.00, 1000.10, 100, 100010, 1),"
            " (%s, 'VERTOZ', 'EQ', 50.00, 50.00, 50.00, 50.00, 2000, 100000, 1)",
            (before, ex_date),
        )
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator)"
            " VALUES ('VERTOZ', 'BE', %s, 'Bonus 1:1', 'bonus', 1, 2)",
            (ex_date,),
        )
        conn.commit()
        day_before, on = queries.adjusted_daily_candles(
            conn, "VERTOZ", start=before, end=ex_date, as_of=ex_date
        )

    assert (day_before.factor, day_before.close, day_before.volume) == (
        Fraction(1, 20),
        D("50.00"),  # 1000.10 / 20 = 50.005, a tie, to the even tick 50.00
        2000,
    )
    assert (on.factor, on.close, on.volume) == (Fraction(1), D("50.00"), 2000)


def test_a_symbol_with_only_dividends_is_unchanged_and_flagged_once_they_are_known(
    conn: psycopg.Connection,
) -> None:
    """INFY's one fixture action is an interim dividend ex 2024-10-29."""
    known = queries.adjusted_daily_candles(
        conn, "INFY", start=START, end=END, as_of=date(2024, 10, 29)
    )
    not_yet = queries.adjusted_daily_candles(
        conn, "INFY", start=START, end=END, as_of=date(2024, 10, 28)
    )
    raw = queries.end_of_day(conn, "INFY", start=START, end=END)

    assert len(known) == len(not_yet) == TRADING_DAYS
    assert [(b.open, b.high, b.low, b.close, b.volume) for b in known] == [
        (r.open, r.high, r.low, r.close, r.traded_qty) for r in raw
    ]
    assert {a.subject for b in known for a in b.unadjusted} == {
        "Interim Dividend - Rs 21 Per Share"
    }
    assert all(b.factor == 1 and b.unadjusted == () for b in not_yet)


def test_a_response_that_is_not_a_list_fails_the_run_and_is_recorded(
    database_url: str, two_runs: TwoRuns
) -> None:
    day = date(2024, 7, 5)
    with app_connection(database_url) as conn:
        with pytest.raises(NseError, match="not a list"):
            ingest_corporate_actions(conn, StubNse(b'{"error": "throttled"}'), start=day, end=day)
        failed = queries.ingest_runs(conn, source="nse_corporate_actions")[0]

    assert failed.error is not None and "NseError" in failed.error
    assert failed.rows_written == 0


def test_a_range_that_ends_before_it_starts_is_refused(database_url: str) -> None:
    with app_connection(database_url) as conn, pytest.raises(ValueError, match="before start"):
        ingest_corporate_actions(conn, StubNse(b"[]"), start=END, end=START)
