"""``compare-adjusted``: our adjusted series against Kite's stored candles, bar by bar.

Each test loads its own database from the recorded fixtures: the master and
end-of-day rows, NSE's corporate actions, and Kite's candles for 2024-07-01
to 2024-08-30, fetched on 2026-10-21. RELIANCE's bars there are halved by its
1:1 bonus of 2024-10-28, which our series reproduces to the paisa. INFY's are
scaled by about 0.98 for a large dividend that we do not adjust.
"""

import json
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from pathlib import Path

import psycopg

from playback import Candle, CandleEdit, Playback, kite_candles
from tradeline.compare_adjusted import AdjustedComparison, compare_adjusted
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.candles import ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.sources.kite import KiteSession
from tradeline.transport import PlaybackTransport, Request
from tradeline.universe import compute_universe

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


def test_infys_large_dividend_shows_as_one_stretch_at_kites_factor_and_fails_nothing(
    database_url: str,
) -> None:
    """Kite scaled every INFY bar of the fixture by about 0.9788 for a dividend we leave alone."""
    with app_connection(database_url) as conn:
        load(conn)
        report = compare_adjusted(conn, symbols=["INFY"], start=START, end=END, as_of=FETCHED_ON)

    (infy,) = report.symbols
    assert (infy.bars, infy.matched, len(infy.mismatches)) == (TRADING_DAYS, 0, TRADING_DAYS)
    assert [(s.first, s.last, s.bars, s.factor) for s in infy.stretches] == [
        (START, END, TRADING_DAYS, Decimal("0.9788"))
    ]
    assert not infy.matches
    assert report.passed  # bars gate nothing; INFY has no split or bonus in the range (#68)


# Factor Agreement (ADR 0018, amended by #68). KPIGREEN's 2 for 1 split of 2024-07-18 is
# played back filed under TCS on 1 August 2024: 22 compared days before it, 21 from it.
# Kite's recorded TCS closes are NSE's own, as if Kite had not adjusted for it. The
# ever-members are RELIANCE, INFY and TCS, by July's traded value.
SPLIT = date(2024, 8, 1)
TCS_TOKEN = "2953217"
EVER_MEMBERS = UniverseRule(
    top_n=3, lookback_trading_days=15, min_listing_trading_days=1, min_presence_percent=50
)


def split_filed_under_tcs(request: Request, body: bytes) -> bytes:
    if "corporates-corporateActions" not in request.url:
        return body
    records = json.loads(body)
    for record in records:
        if record["symbol"] == "KPIGREEN" and "Split" in record["subject"]:
            record.update(symbol="TCS", isin="INE467B01029", exDate="01-Aug-2024")
    return json.dumps(records).encode()


def load_split(conn: psycopg.Connection, edit: CandleEdit | None = None) -> None:
    """The fixtures with the split filed under TCS, and the ever-members stored."""
    ingest_eod(conn, PlaybackTransport(FIXTURES / "ingest_eod"), start=START, end=END)
    ingest_corporate_actions(
        conn,
        Playback(FIXTURES / "ingest_corporate_actions", edit=split_filed_under_tcs),
        start=ACTIONS_START,
        end=ACTIONS_END,
    )
    candles = FIXTURES / "ingest_candles"
    transport = (
        PlaybackTransport(candles) if edit is None else Playback(candles, edit=kite_candles(edit))
    )
    session = KiteSession(transport, api_key="key", access_token="tok", sleep=lambda _: None)
    ingest_candles(conn, session, start=START, end=END, symbols=["RELIANCE", "INFY", "TCS"])
    rebuild_instruments(conn)
    compute_universe(conn, EVER_MEMBERS, month=date(2024, 8, 1))


def outcomes(report: AdjustedComparison) -> list[tuple[object, ...]]:
    return [
        (a.symbols, j.ex_date, j.outcome, j.factor, j.kite_factor, j.reason)
        for a, j in report.judged
    ]


def test_a_split_kites_closes_carry_straight_across_disagrees_and_fails_the_run(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_split(conn)
        report = compare_adjusted(conn, symbols=None, start=START, end=END, as_of=FETCHED_ON)

    assert outcomes(report) == [(("TCS",), SPLIT, "disagrees", Fraction(1, 2), Fraction(1), None)]
    assert report.symbols == ()  # no bars listed unless a Symbol is named
    assert not report.passed
    assert report.summary()["factor_agreement"] == {
        "agrees": 0,
        "disagrees": 1,
        "not_judged": 0,
        "not_judged_by_reason": {},
        "ex_dates": [
            {
                "instrument": "TCS@2004-08-25",
                "symbols": ["TCS"],
                "ex_date": "2024-08-01",
                "outcome": "disagrees",
                "factor": "0.5",
                "kite_factor": "1",
                "reason": None,
            }
        ],
        "unresolved_members": [],
    }
    assert "  TCS 2024-08-01 DISAGREES: ours 0.5, Kite's 1\n" in report.text()


def halved_before_the_split(request: Request, candles: list[Candle]) -> list[Candle]:
    """TCS's closes before the split halved to the 5 paisa tick, as Kite adjusts for it."""
    if f"/historical/{TCS_TOKEN}/" not in request.url:
        return candles
    for candle in candles:
        if date.fromisoformat(candle[0][:10]) < SPLIT:
            halved = (Decimal(str(candle[4])) / 2 / Decimal("0.05")).quantize(
                Decimal(1), ROUND_HALF_EVEN
            ) * Decimal("0.05")
            candle[1:5] = [float(halved)] * 4
    return candles


def test_a_split_kite_adjusted_by_our_factor_agrees_and_a_named_symbol_lists_its_bars(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_split(conn, halved_before_the_split)
        report = compare_adjusted(conn, symbols=["TCS"], start=START, end=END, as_of=FETCHED_ON)

    assert outcomes(report) == [(("TCS",), SPLIT, "agrees", Fraction(1, 2), Fraction(1, 2), None)]
    (tcs,) = report.symbols
    assert (tcs.symbol, tcs.bars) == ("TCS", TRADING_DAYS)
    assert report.passed


def divided_by_three_before_the_split(request: Request, candles: list[Candle]) -> list[Candle]:
    """TCS's closes before the split divided by 3, a ratio other than the stored 2 for 1."""
    if f"/historical/{TCS_TOKEN}/" not in request.url:
        return candles
    for candle in candles:
        if date.fromisoformat(candle[0][:10]) < SPLIT:
            third = (Decimal(str(candle[4])) / 3 / Decimal("0.05")).quantize(
                Decimal(1), ROUND_HALF_EVEN
            ) * Decimal("0.05")
            candle[1:5] = [float(third)] * 4
    return candles


def test_a_split_kite_divided_by_another_ratio_disagrees_and_fails_the_run(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_split(conn, divided_by_three_before_the_split)
        report = compare_adjusted(conn, symbols=None, start=START, end=END, as_of=FETCHED_ON)

    ((_, judgement),) = report.judged
    assert (judgement.ex_date, judgement.outcome) == (SPLIT, "disagrees")
    assert abs(1 / _of(judgement.kite_factor) - 3) < Fraction(1, 2000)
    assert not report.passed


def before_the_split(request: Request, candles: list[Candle]) -> list[Candle]:
    """TCS's candles up to the day before the split, as if fetched then."""
    if f"/historical/{TCS_TOKEN}/" not in request.url:
        return candles
    return [c for c in candles if date.fromisoformat(c[0][:10]) < SPLIT]


def test_an_ex_date_not_judged_is_listed_with_its_reason_and_does_not_fail_the_run(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_split(conn, before_the_split)
        report = compare_adjusted(conn, symbols=None, start=START, end=END, as_of=FETCHED_ON)

    assert outcomes(report) == [
        (("TCS",), SPLIT, "not_judged", Fraction(1, 2), None, "no_closes_after")
    ]
    assert report.passed
    assert "  TCS 2024-08-01 not judged: no_closes_after\n" in report.text()


def test_a_named_symbol_is_judged_as_its_instrument_and_every_ex_date_is_printed(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        load_split(conn, halved_before_the_split)
        report = compare_adjusted(conn, symbols=["TCS"], start=START, end=END, as_of=FETCHED_ON)

    ((agreement, _),) = report.judged
    assert agreement.instrument == "TCS@2004-08-25"
    assert "  TCS 2024-08-01 agrees: ours 0.5\n" in report.text()


def _of[T](value: T | None) -> T:
    assert value is not None
    return value
