"""``compare-adjusted``: our adjusted series against Kite's stored candles, bar by bar.

Each test loads its own database from the recorded fixtures: the master and
end-of-day rows, NSE's corporate actions, and Kite's candles for 2024-07-01
to 2024-08-30, fetched on 2026-10-21. RELIANCE's bars there are halved by its
1:1 bonus of 2024-10-28, which our series reproduces to the paisa. INFY's are
scaled by about 0.98 for a large dividend that we do not adjust.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg

from playback import CandleEdit, Playback, kite_candles
from tradeline.compare_adjusted import compare_adjusted
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport

FIXTURES = Path(__file__).parent / "fixtures"
START, END = date(2024, 7, 1), date(2024, 8, 30)
ACTIONS_START, ACTIONS_END = date(2024, 7, 1), date(2024, 12, 31)
FETCHED_ON = date(2026, 10, 21)
TRADING_DAYS = 43
BONUS_EX_DATE = date(2024, 10, 28)
RELIANCE_TOKEN = "738561"


def load(conn: psycopg.Connection, edit: CandleEdit | None = None) -> None:
    ingest_eod(conn, PlaybackTransport(FIXTURES / "ingest_eod"), start=START, end=END)
    ingest_corporate_actions(
        conn,
        PlaybackTransport(FIXTURES / "ingest_corporate_actions"),
        start=ACTIONS_START,
        end=ACTIONS_END,
    )
    candles = FIXTURES / "ingest_candles"
    transport = (
        PlaybackTransport(candles) if edit is None else Playback(candles, edit=kite_candles(edit))
    )
    session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
    ingest_candles(conn, session, start=START, end=END, symbols=["RELIANCE", "INFY"])


def test_reliance_across_its_bonus_matches_kite_on_every_bar(database_url: str) -> None:
    with app_connection(database_url) as conn:
        load(conn)
        report = compare_adjusted(
            conn, symbols=["RELIANCE"], start=START, end=END, as_of=FETCHED_ON
        )

    (reliance,) = report.symbols
    assert (reliance.symbol, reliance.bars, reliance.matched) == ("RELIANCE", TRADING_DAYS, 43)
    assert (reliance.mismatches, reliance.only_kite, reliance.only_ours) == ((), (), ())
    assert [(a.ex_date, a.kind) for a in reliance.adjusting_actions] == [(BONUS_EX_DATE, "bonus")]
    assert reliance.matches and report.passed


def test_infys_large_dividend_shows_as_one_stretch_at_kites_factor(database_url: str) -> None:
    """Kite scaled every INFY bar of the fixture by about 0.9788 for a dividend we leave alone."""
    with app_connection(database_url) as conn:
        load(conn)
        report = compare_adjusted(conn, symbols=["INFY"], start=START, end=END, as_of=FETCHED_ON)

    (infy,) = report.symbols
    assert (infy.bars, infy.matched, len(infy.mismatches)) == (TRADING_DAYS, 0, TRADING_DAYS)
    assert [(s.first, s.last, s.bars, s.factor) for s in infy.stretches] == [
        (START, END, TRADING_DAYS, Decimal("0.9788"))
    ]
    assert not infy.matches and not report.passed
