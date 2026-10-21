"""Kite's daily candles by Instrument across a rename: fetched, read, and reconciled.

The evidence is recorded: NSE's end-of-day files for 7 to 11 April 2025
trimmed to ZOMATO and ETERNAL (ZOMATO trades to the 8th, ETERNAL from the
9th), NSE's equity master, and the symbol-change list (``fixtures/instruments/``,
see its README). Kite's side is ``fixtures/candles_rename/`` (see its README):
Kite's instrument dump of 2026-10-21, which lists only ETERNAL (token 1304833),
and ETERNAL's daily candles over the same week, which Kite returns for every
day, the two before the rename included, all under the one token.

Kite's dump before the rename listed the token as ZOMATO. That dump is the
recorded one played back with ETERNAL's line relabelled ZOMATO
(``playback.lines``), and a copy fetched on another day under another
adjustment is the recorded response with a close moved (``playback``), so no
fixture is edited by hand.
"""

from datetime import date
from pathlib import Path

import psycopg

from playback import Edit, Playback, kite_candles, lines, shift_close
from tradeline import queries
from tradeline.config import Config
from tradeline.db import app_connection
from tradeline.ingest.candles import CandleIngestResult, ingest_candles
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.reconcile import ReconcileReport, reconcile
from tradeline.sources.kite import INSTRUMENTS_URL, KiteSession
from tradeline.transport import PlaybackTransport, Request

FIXTURES_DIR = Path(__file__).parent / "fixtures"
KITE = FIXTURES_DIR / "candles_rename"
NSE = FIXTURES_DIR / "instruments"
START, END = date(2025, 4, 7), date(2025, 4, 11)
ETERNAL = "ZOMATO@2021-07-23"
ETERNAL_TOKEN = "1304833"
SETTINGS = Config.Reconcile(close_tolerance=0.001, max_discrepancy_pct=0.1)
# Kite's candles as recorded: (day, close).
KITE_CLOSES = [
    (date(2025, 4, 7), "209.63"),
    (date(2025, 4, 8), "215.19"),
    (date(2025, 4, 9), "211.39"),
    (date(2025, 4, 11), "217.2"),
]


def as_before_the_rename(request: Request, body: bytes) -> bytes:
    """Kite's dump as it stood before the rename: ETERNAL's token listed as ZOMATO."""
    if request.url != INSTRUMENTS_URL:
        return body
    relabel = lines(lambda rows: [row.replace(",ETERNAL,", ",ZOMATO,") for row in rows])
    return relabel(request, body)


def load_nse(conn: psycopg.Connection, dump: Edit | None = None) -> None:
    """NSE's end-of-day rows and symbol changes, Kite's dump giving the tokens."""
    ingest_eod(conn, Playback(KITE, NSE, edit=dump), start=START, end=END)
    ingest_symbol_changes(conn, PlaybackTransport(NSE))


def fetch(conn: psycopg.Connection, transport: Playback, *symbols: str) -> CandleIngestResult:
    session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
    return ingest_candles(conn, session, start=START, end=END, symbols=list(symbols))


def closes(candles: list[queries.DailyCandle]) -> list[tuple[str, date, str]]:
    return [(c.symbol, c.trade_date, str(c.close)) for c in candles]


def test_an_old_symbol_is_fetched_through_the_current_symbols_token_and_stored_under_it(
    fresh_database_url: str,
) -> None:
    kite = Playback(KITE)
    with app_connection(fresh_database_url) as conn:
        load_nse(conn)
        rebuild_instruments(conn)
        result = fetch(conn, kite, "ZOMATO")
        eternal = queries.daily_candles(conn, "ETERNAL", start=START, end=END)
        zomato = queries.daily_candles(conn, "ZOMATO", start=START, end=END)
        (run,) = queries.ingest_runs(conn, source="kite_candles_daily")

    assert [f"/historical/{ETERNAL_TOKEN}/day" in call.url for call in kite.calls] == [True]
    assert (result.symbols_loaded, result.symbols_without_token) == (1, ())
    assert run.details["fetched_through"] == {"ZOMATO": "ETERNAL"}
    assert closes(eternal) == [("ETERNAL", day, close) for day, close in KITE_CLOSES]
    assert zomato == []


def reconciled(conn: psycopg.Connection) -> ReconcileReport:
    return reconcile(conn, SETTINGS, start=START, end=END)


def test_reconcile_by_instrument_counts_no_extras_for_a_rename_that_by_symbol_shows(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_nse(conn)
        fetch(conn, Playback(KITE), "ETERNAL")
        by_symbol = reconciled(conn)
        rebuild_instruments(conn)
        by_instrument = reconciled(conn)
        by_old_name = reconcile(conn, SETTINGS, start=START, end=END, symbols=["ZOMATO"])

    # Kite's two bars from before the rename are ETERNAL's; NSE named those days ZOMATO.
    assert [(d.symbol, d.trade_date) for d in by_symbol.extra] == [
        ("ETERNAL", date(2025, 4, 7)),
        ("ETERNAL", date(2025, 4, 8)),
    ]
    assert (by_instrument.rows, by_instrument.discrepancies) == (4, 0)
    # Naming the old Symbol compares its Instrument, whose candles are under the new one.
    assert (by_old_name.rows, by_old_name.discrepancies) == (4, 0)


def test_an_instrument_day_stored_under_two_kite_names_reads_the_newer_and_reports_a_disagreement(
    fresh_database_url: str,
) -> None:
    # Fetched under ZOMATO before the rename, then again as ETERNAL after Kite
    # adjusted the 8th's close by a rupee: four days under each name.
    readjusted = Playback(KITE, edit=kite_candles(shift_close(ETERNAL_TOKEN, "2025-04-08", "-1")))
    with app_connection(fresh_database_url) as conn:
        load_nse(conn, dump=as_before_the_rename)
        fetch(conn, Playback(KITE), "ZOMATO")
        load_nse(conn)
        rebuild_instruments(conn)
        fetch(conn, readjusted, "ETERNAL")
        read = queries.instrument_daily_candles(conn, ETERNAL, start=START, end=END)
        report = reconciled(conn)

    assert closes(read.candles) == [
        ("ETERNAL", date(2025, 4, 7), "209.63"),
        ("ETERNAL", date(2025, 4, 8), "214.19"),
        ("ETERNAL", date(2025, 4, 9), "211.39"),
        ("ETERNAL", date(2025, 4, 11), "217.2"),
    ]
    (disagreement,) = read.disagreements
    assert (disagreement.kept.symbol, disagreement.other.symbol) == ("ETERNAL", "ZOMATO")
    assert (disagreement.trade_date, disagreement.series) == (date(2025, 4, 8), "EQ")
    assert (str(disagreement.kept.close), str(disagreement.other.close)) == ("214.19", "215.19")
    # Reconcile compares one candle per Instrument-day, the newer, and reports the other.
    assert (report.rows, len(report.missing), len(report.extra)) == (4, 0, 0)
    assert report.candle_disagreements == (disagreement,)
    summary = report.summary()
    assert summary["candle_disagreements"] == 1
    assert summary["listed_candle_disagreements"] == [
        {
            "instrument": ETERNAL,
            "series": "EQ",
            "trade_date": "2025-04-08",
            "kept_symbol": "ETERNAL",
            "kept_close": "214.19",
            "other_symbol": "ZOMATO",
            "other_close": "215.19",
        }
    ]
    assert "1 candle disagreement" in report.text()


def test_an_instruments_candles_read_through_a_symbol_are_those_of_the_instrument_it_named(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_nse(conn)
        rebuild_instruments(conn)
        fetch(conn, Playback(KITE), "ETERNAL")
        by_key = queries.instrument_daily_candles(conn, ETERNAL, start=START, end=END)
        by_new_name = queries.instrument_daily_candles(conn, "ETERNAL", start=START, end=END)
        # ZOMATO on the day its rows end names the Instrument now trading as ETERNAL.
        by_old_name = queries.instrument_daily_candles(
            conn, "ZOMATO", start=START, end=END, as_of=date(2025, 4, 8)
        )

    assert closes(by_key.candles) == [("ETERNAL", day, close) for day, close in KITE_CLOSES]
    assert by_new_name == by_key
    assert by_old_name == by_key
