"""Why an Instrument has no Kite candles: the reason it ended, never "missing data".

The fixtures (``fixtures/end_reasons/``, see its README) are NSE's
end-of-day files for 13 May to 10 June 2019 trimmed to four Symbols, NSE's
corporate actions over the same range trimmed to theirs, and NSE's
delisted-securities list. Kite's dump and NSE's equity master are
``fixtures/ingest_eod/``'s, which list none of the four, so no Kite token is
found for any of them and each Instrument is keyed by its first row (13 May
2019, ANGIND's 14 May). The Raw Series so runs to 10 June 2019, and:

- IVRCLINFRA trades in EQ to 6 June and in BE from 7 June: its series moved;
- PENPEBS's last row is 6 June, and NSE lists a "Scheme Of Amalgamation" for
  it ex 7 June: merged, on NSE's word;
- ANGIND (BZ) ends on 4 June, and NSE's delisted list records it delisted on
  liquidation on 10 July 2020;
- BILPOWER (BZ) ends on 13 May, with no record saying why.

The Universe for June 2019 is computed under a rule ranking Instruments with
its windows shrunk to five trading days, so PENPEBS and IVRCLINFRA (series EQ)
are members.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import psycopg
import pytest

from playback import Playback
from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.candles import CurrentUniverse, ingest_candles
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.delistings import ingest_delistings
from tradeline.ingest.eod import ingest_eod
from tradeline.instruments_report import instruments_report
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.sources.kite import KiteSession
from tradeline.transport import RefusingTransport, Request
from tradeline.universe import compute_universe

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = FIXTURES_DIR / "end_reasons"
EOD_FIXTURES = FIXTURES_DIR / "ingest_eod"
# Its recorded warm-up request answers this directory's corporate-action request too.
CA_FIXTURES = FIXTURES_DIR / "ingest_corporate_actions"
START, END = date(2019, 5, 13), date(2019, 6, 10)
JUNE = date(2019, 6, 1)

RULE = replace(
    UniverseRule(
        top_n=5, lookback_trading_days=5, min_listing_trading_days=5, min_presence_percent=90
    ),
    rank_by="instrument",
)

IVRCLINFRA = "IVRCLINFRA@2019-05-13"
PENPEBS = "PENPEBS@2019-05-13"
ANGIND = "ANGIND@2019-05-14"  # BZ trades on fewer days; its first is the 14th
BILPOWER = "BILPOWER@2019-05-13"


def without_scheme(request: Request, body: bytes) -> bytes:
    """NSE's corporate actions played back without PENPEBS's amalgamation."""
    if "corporates-corporateActions" in request.url:
        return b"[]"
    return body


def load(url: str, *, corporate_actions: Playback) -> None:
    with app_connection(url) as conn:
        ingest_eod(conn, Playback(FIXTURES, EOD_FIXTURES), start=START, end=END)
        ingest_corporate_actions(conn, corporate_actions, start=START, end=END)
        ingest_delistings(conn, Playback(FIXTURES))
        rebuild_instruments(conn)
        compute_universe(conn, RULE, month=JUNE)


@pytest.fixture(scope="module")
def loaded(database_url: str) -> str:
    load(database_url, corporate_actions=Playback(FIXTURES, CA_FIXTURES))
    return database_url


@pytest.fixture
def conn(loaded: str) -> Iterator[psycopg.Connection]:
    with app_connection(loaded) as conn:
        yield conn


def reasons(conn: psycopg.Connection, *wanted: tuple[str, str]) -> list[tuple[object, ...]]:
    return [
        (r.instrument, r.series, r.reason, r.last_day, r.last_symbol, r.detail)
        for r in queries.end_reasons(conn, wanted)
    ]


def test_each_instrument_gets_the_reason_its_rows_and_nses_records_give(
    conn: psycopg.Connection,
) -> None:
    assert reasons(
        conn,
        (IVRCLINFRA, "EQ"),
        (IVRCLINFRA, "BE"),
        (PENPEBS, "EQ"),
        (ANGIND, "BZ"),
        (BILPOWER, "BZ"),
    ) == [
        (
            IVRCLINFRA,
            "EQ",
            "series moved",
            date(2019, 6, 6),
            "IVRCLINFRA",
            "continues in BE from 2019-06-07",
        ),
        (IVRCLINFRA, "BE", "still trading", date(2019, 6, 10), "IVRCLINFRA", ""),
        (
            PENPEBS,
            "EQ",
            "merged",
            date(2019, 6, 6),
            "PENPEBS",
            "NSE corporate action ex 2019-06-07: Scheme Of Amalgamation",
        ),
        (
            ANGIND,
            "BZ",
            "delisted",
            date(2019, 6, 4),
            "ANGIND",
            "NSE delisted list: Delisting - Liquidation on 2020-07-10",
        ),
        (BILPOWER, "BZ", "ended, reason not stated", date(2019, 5, 13), "BILPOWER", ""),
    ]


def test_nothing_is_called_merged_without_nses_record(fresh_database_url: str) -> None:
    load(
        fresh_database_url,
        corporate_actions=Playback(FIXTURES, CA_FIXTURES, edit=without_scheme),
    )
    with app_connection(fresh_database_url) as conn:
        assert reasons(conn, (PENPEBS, "EQ")) == [
            (PENPEBS, "EQ", "ended, reason not stated", date(2019, 6, 6), "PENPEBS", "")
        ]


def test_the_report_gives_each_ended_instrument_its_reason(conn: psycopg.Connection) -> None:
    report = instruments_report(conn)

    assert [(e.key, e.last_day, e.reason, e.detail) for e in report.ended] == [
        (
            ANGIND,
            date(2019, 6, 4),
            "delisted",
            "NSE delisted list: Delisting - Liquidation on 2020-07-10",
        ),
        (BILPOWER, date(2019, 5, 13), "ended, reason not stated", ""),
        (
            PENPEBS,
            date(2019, 6, 6),
            "merged",
            "NSE corporate action ex 2019-06-07: Scheme Of Amalgamation",
        ),
    ]


def test_the_report_lists_the_members_without_kite_candles_by_reason(
    conn: psycopg.Connection,
) -> None:
    report = instruments_report(conn)

    assert [(r.reason, r.instrument, r.series) for r in report.without_candles] == [
        ("merged", PENPEBS, "EQ"),
        ("series moved", IVRCLINFRA, "EQ"),
    ]
    assert report.summary()["without_candles"] == {"merged": 1, "series moved": 1}
    text = report.text()
    assert (
        "Members without Kite candles: 2\n"
        "  by reason: key  series  last day  last Symbol  detail\n"
        "  merged: 1\n"
        f"    {PENPEBS}  EQ  2019-06-06  PENPEBS"
        "  NSE corporate action ex 2019-06-07: Scheme Of Amalgamation\n"
        "  series moved: 1\n"
        f"    {IVRCLINFRA}  EQ  2019-06-06  IVRCLINFRA  continues in BE from 2019-06-07\n"
    ) in text
    assert f"  {BILPOWER}  2019-05-13  BILPOWER  ended, reason not stated\n" in text


def no_kite() -> KiteSession:
    # No member has a token, so nothing is fetched.
    return KiteSession(RefusingTransport(), api_key="key", access_token="tok", sleep=lambda _: None)


def test_ingest_candles_lists_the_universe_members_without_a_token_by_reason(
    conn: psycopg.Connection,
) -> None:
    result = ingest_candles(
        conn, no_kite(), start=START, end=END, universe=CurrentUniverse(JUNE, RULE.version)
    )
    run = queries.ingest_runs(conn, source="kite_candles_daily")[0]

    assert (result.symbols_loaded, result.symbols_without_token) == (0, ("IVRCLINFRA", "PENPEBS"))
    expected = {
        "series moved": [
            {
                "symbol": "IVRCLINFRA",
                "requested": "IVRCLINFRA",
                "instrument": IVRCLINFRA,
                "detail": "continues in BE from 2019-06-07",
            }
        ],
        "merged": [
            {
                "symbol": "PENPEBS",
                "requested": "PENPEBS",
                "instrument": PENPEBS,
                "detail": "NSE corporate action ex 2019-06-07: Scheme Of Amalgamation",
            }
        ],
    }
    assert result.summary()["without_token_by_reason"] == expected
    assert run.details["without_token_by_reason"] == expected


def test_ingest_candles_gives_named_symbols_without_a_token_their_reasons(
    conn: psycopg.Connection,
) -> None:
    result = ingest_candles(
        conn, no_kite(), start=START, end=END, symbols=["BILPOWER", "ANGIND", "IVRCLINFRA"]
    )

    assert {
        reason: [(m.symbol, m.instrument) for m in members]
        for reason, members in result.without_token_by_reason.items()
    } == {
        "delisted": [("ANGIND-BZ", ANGIND)],
        "ended, reason not stated": [("BILPOWER-BZ", BILPOWER)],
        "still trading": [("IVRCLINFRA-BE", IVRCLINFRA)],
        "series moved": [("IVRCLINFRA", IVRCLINFRA)],
    }
