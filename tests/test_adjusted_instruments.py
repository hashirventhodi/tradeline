"""The end-of-day, Corporate Action, and Adjusted Series reads of an Instrument.

The evidence is recorded (``fixtures/adjusted_instruments/``, see its README):
Gujarat Gas, which split its shares 5 for 1 on 15 January 2019 (a new ISIN,
INE844O01022 to INE844O01030, from the 16th) and was renamed GUJGASLTD to
GUJENERGY on 1 July 2026. NSE's end-of-day files for 14 to 16 January 2019
and 29 June to 1 July 2026 are trimmed to its rows, the equity master to
GUJENERGY, and the symbol-change list to its rename. NSE's Corporate Action
list now files the 2019 split under GUJENERGY, the Symbol of today; to read
it as it was filed before the rename, the recording is played back with the
action under GUJGASLTD (``filed_under``). The www warm-up comes from
``fixtures/ingest_corporate_actions/`` and Kite's instrument list from
``fixtures/ingest_eod/``.

The adjusted prices are worked by hand from the raw rows: before the split
every price is divided by 5 to the 5 paisa tick and the volume multiplied by 5.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import psycopg
import pytest

from playback import Playback
from tradeline import queries
from tradeline.db import app_connection
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.transport import Request

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = FIXTURES_DIR / "adjusted_instruments"
EOD_FIXTURES = FIXTURES_DIR / "ingest_eod"
CA_FIXTURES = FIXTURES_DIR / "ingest_corporate_actions"
SPLIT = date(2019, 1, 15)  # 5 for 1, Rs 10 to Rs 2
RENAMED = date(2026, 7, 1)  # GUJGASLTD to GUJENERGY
FIRST, LAST = date(2019, 1, 14), RENAMED
KEY = "GUJGASLTD@2015-09-15"
D = Decimal


def filed_under(symbol: str) -> Playback:
    """NSE's Corporate Action list with the split filed under ``symbol``, as before the rename."""

    def edit(request: Request, body: bytes) -> bytes:
        if "corporates-corporateActions" not in request.url:
            return body
        return body.replace(b'"symbol":"GUJENERGY"', f'"symbol":"{symbol}"'.encode())

    return Playback(FIXTURES, CA_FIXTURES, edit=edit)


def load(url: str, *actions: Playback) -> None:
    """The end-of-day rows, the rename, each Corporate Action list given, then a rebuild."""
    with app_connection(url) as conn:
        eod = Playback(FIXTURES, EOD_FIXTURES)
        ingest_eod(conn, eod, start=FIRST, end=date(2019, 1, 16))
        ingest_eod(conn, eod, start=date(2026, 6, 29), end=LAST)
        for transport in actions:
            ingest_corporate_actions(
                conn, transport, start=FIRST, end=date(2019, 1, 16), symbols=["GUJENERGY"]
            )
        ingest_symbol_changes(conn, Playback(FIXTURES))
        rebuild_instruments(conn)


@pytest.fixture(scope="module")
def conn(database_url: str) -> Iterator[psycopg.Connection]:
    """The split filed under the old Symbol, GUJGASLTD."""
    load(database_url, filed_under("GUJGASLTD"))
    with app_connection(database_url) as conn:
        yield conn


def test_the_adjusted_series_of_the_new_symbol_runs_back_through_the_old_symbols_rows(
    conn: psycopg.Connection,
) -> None:
    bars = queries.adjusted_daily_candles(conn, "GUJENERGY", start=FIRST, end=LAST, as_of=LAST)

    assert [(b.trade_date, b.symbol, b.close, b.volume, b.factor) for b in bars] == [
        (date(2019, 1, 14), "GUJGASLTD", D("133.85"), 117_250, Fraction(1, 5)),
        (SPLIT, "GUJGASLTD", D("143.3"), 2_995_340, 1),
        (date(2019, 1, 16), "GUJGASLTD", D("143.3"), 523_071, 1),
        (date(2026, 6, 29), "GUJGASLTD", D("335.30"), 1_519_737, 1),
        (date(2026, 6, 30), "GUJGASLTD", D("327.05"), 1_750_950, 1),
        (RENAMED, "GUJENERGY", D("340.00"), 2_029_887, 1),
    ]


def test_the_split_as_nse_files_it_today_under_the_new_symbol_adjusts_the_old_symbols_bars(
    fresh_database_url: str,
) -> None:
    load(fresh_database_url, Playback(FIXTURES, CA_FIXTURES))
    with app_connection(fresh_database_url) as conn:
        bars = queries.adjusted_daily_candles(conn, "GUJGASLTD", start=FIRST, end=SPLIT, as_of=LAST)

    assert [(b.trade_date, b.symbol, b.close, b.factor) for b in bars] == [
        (date(2019, 1, 14), "GUJGASLTD", D("133.85"), Fraction(1, 5)),
        (SPLIT, "GUJGASLTD", D("143.3"), 1),
    ]


def test_a_split_filed_under_both_symbols_is_applied_once(fresh_database_url: str) -> None:
    load(fresh_database_url, Playback(FIXTURES, CA_FIXTURES), filed_under("GUJGASLTD"))
    with app_connection(fresh_database_url) as conn:
        filed = queries.corporate_actions(conn, KEY)
        (bar, *_) = queries.adjusted_daily_candles(conn, KEY, start=FIRST, end=FIRST, as_of=LAST)

    assert [(a.symbol, a.ex_date, a.factor) for a in filed] == [
        ("GUJENERGY", SPLIT, Fraction(1, 5)),
        ("GUJGASLTD", SPLIT, Fraction(1, 5)),
    ]
    assert (bar.close, bar.volume, bar.factor) == (D("133.85"), 117_250, Fraction(1, 5))


def test_a_split_that_issues_a_new_isin_gives_one_unbroken_series(
    conn: psycopg.Connection,
) -> None:
    rows = queries.end_of_day(conn, KEY, start=FIRST, end=date(2019, 1, 16))
    bars = queries.adjusted_daily_candles(conn, KEY, start=FIRST, end=date(2019, 1, 16), as_of=LAST)

    assert [(r.trade_date, r.isin) for r in rows] == [
        (date(2019, 1, 14), "INE844O01022"),
        (SPLIT, "INE844O01022"),
        (date(2019, 1, 16), "INE844O01030"),
    ]
    assert [(b.trade_date, b.open, b.close) for b in bars] == [
        (date(2019, 1, 14), D("134.00"), D("133.85")),  # 669.95 and 669.25, divided by 5
        (SPLIT, D("135.5"), D("143.3")),
        (date(2019, 1, 16), D("144.95"), D("143.3")),
    ]


def test_an_instruments_reads_by_key_or_by_any_of_its_symbols_agree(
    conn: psycopg.Connection,
) -> None:
    by_key = queries.end_of_day(conn, KEY, start=FIRST, end=LAST)

    assert queries.end_of_day(conn, "GUJGASLTD", start=FIRST, end=LAST) == by_key
    assert queries.end_of_day(conn, "GUJENERGY", start=FIRST, end=LAST) == by_key
    assert [(r.trade_date, r.symbol) for r in by_key][-2:] == [
        (date(2026, 6, 30), "GUJGASLTD"),
        (RENAMED, "GUJENERGY"),
    ]
    assert queries.corporate_actions(conn, "GUJENERGY") == queries.corporate_actions(conn, KEY)
    (action,) = queries.corporate_actions(conn, KEY)
    assert (action.symbol, action.ex_date) == ("GUJGASLTD", SPLIT)


def test_an_unknown_instrument_key_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(LookupError, match="no Instrument GUJENERGY@2026-07-01"):
        queries.adjusted_daily_candles(
            conn, "GUJENERGY@2026-07-01", start=FIRST, end=LAST, as_of=LAST
        )


def test_no_action_after_the_as_of_date_applies_and_no_bar_after_it_returns(
    conn: psycopg.Connection,
) -> None:
    before = date(2019, 1, 14)
    (bar,) = queries.adjusted_daily_candles(
        conn, "GUJGASLTD", start=FIRST, end=before, as_of=before
    )

    assert (bar.close, bar.volume, bar.factor) == (D("669.25"), 23_450, 1)
    with pytest.raises(ValueError, match="after the as-of date"):
        queries.adjusted_daily_candles(conn, KEY, start=FIRST, end=LAST, as_of=before)


def test_a_symbol_is_refused_as_of_a_date_before_it_named_the_instrument(
    conn: psycopg.Connection,
) -> None:
    before = date(2026, 6, 30)
    with pytest.raises(LookupError, match="GUJENERGY named no Instrument on or before 2026-06-30"):
        queries.adjusted_daily_candles(conn, "GUJENERGY", start=FIRST, end=before, as_of=before)
