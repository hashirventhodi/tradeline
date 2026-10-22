"""``ingest cited-actions``: reviewed rows citing NSE's circulars, checked against the price.

The end-of-day rows are written out here with KARDA's real dates, ISINs, and
closes around its 2020 split (BE, the trade-for-trade series): 275.65 on the
last day before the Ex-Date, 57.90 on the Ex-Date, 2020-07-02, the old ISIN's
last row, and the new ISIN from 2020-07-03. GICL's split and bonus of
2025-10-15 (SM, 173.70 to 46.50) are two rows checked together.
"""

import json
import logging
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from fractions import Fraction

import psycopg
import pytest

from test_corporate_actions import StubNse
from test_corporate_actions import record as nse_record
from tradeline import queries
from tradeline.adjust import interpret
from tradeline.cited_actions import CitedAction
from tradeline.db import app_connection
from tradeline.ingest.cited_actions import ingest_cited_actions
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.instruments import ManualLink
from tradeline.instruments_report import instruments_report
from tradeline.rebuild_instruments import rebuild_instruments

D = Decimal
KARDA_OLD, KARDA_NEW = "INE278R01018", "INE278R01026"
KARDA_EX_DATE = date(2020, 7, 2)
KARDA_SPLIT = "Sub-Division from Rs. 10/- to Rs. 2/-"
KARDA_CIRCULAR = "https://nsearchives.nseindia.com/content/circulars/CML44835.pdf"
GICL_EX_DATE = date(2025, 10, 15)
GICL_CIRCULAR = "https://nsearchives.nseindia.com/content/circulars/CMTR70599.pdf"


def karda_split(*, ex_date: date = KARDA_EX_DATE, subject: str = KARDA_SPLIT) -> CitedAction:
    kind, factor = interpret(subject)
    assert factor is not None
    return CitedAction("KARDA", ex_date, subject, KARDA_CIRCULAR, kind, factor, 2)


def eod(conn: psycopg.Connection, rows: Iterable[tuple[date, str, str, str, str]]) -> None:
    """End-of-day rows of (day, Symbol, series, ISIN, close), every price at the close."""
    for day, symbol, series, isin, close in rows:
        conn.execute(
            "INSERT INTO eod_bhavcopy (trade_date, symbol, series, isin, open, high, low, close,"
            " traded_qty, traded_value, trades) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 100, 1, 1)",
            (day, symbol, series, isin, close, close, close, close),
        )
    conn.commit()


def karda_rows(conn: psycopg.Connection) -> None:
    eod(
        conn,
        [
            (date(2020, 6, 30), "KARDA", "BE", KARDA_OLD, "272.00"),
            (date(2020, 7, 1), "KARDA", "BE", KARDA_OLD, "275.65"),
            (KARDA_EX_DATE, "KARDA", "BE", KARDA_OLD, "57.90"),
            (date(2020, 7, 3), "KARDA", "BE", KARDA_NEW, "58.00"),
        ],
    )


def stored(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT symbol, series, ex_date, subject, kind, factor_numerator, factor_denominator,"
        " nse_list, citation FROM corporate_actions ORDER BY symbol, ex_date, subject"
    ).fetchall()


def test_a_row_the_price_agrees_with_is_stored_with_its_citation_and_no_list(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)

        result = ingest_cited_actions(conn, [karda_split()])

        assert stored(conn) == [
            ("KARDA", "BE", KARDA_EX_DATE, KARDA_SPLIT, "split", 1, 5, None, KARDA_CIRCULAR)
        ]
    assert (result.rows_seen, result.rows_written, result.refused) == (1, 1, 0)


def test_a_stored_row_joins_the_isin_change_and_leaves_no_fall_in_the_adjusted_series(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        ingest_cited_actions(conn, [karda_split()])

        rebuilt = rebuild_instruments(conn)
        (link,) = queries.instrument_history(conn, "KARDA@2020-06-30").links
        bars = queries.adjusted_daily_candles(
            conn, "KARDA", start=date(2020, 7, 1), end=date(2020, 7, 3), as_of=date(2020, 7, 3)
        )

    assert rebuilt.anomalies == 0
    assert (link.kind, link.from_isin, link.to_isin) == ("corporate_action", KARDA_OLD, KARDA_NEW)
    assert KARDA_CIRCULAR in link.source
    # 275.65 / 5 = 55.13, to the 5 paisa tick.
    assert [b.close for b in bars] == [D("55.15"), D("57.90"), D("58.00")]


def test_a_second_run_of_the_same_rows_writes_nothing(fresh_database_url: str) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        ingest_cited_actions(conn, [karda_split()])

        again = ingest_cited_actions(conn, [karda_split()])

    assert (again.rows_seen, again.rows_written, again.refused) == (1, 0, 0)


def test_a_row_whose_factor_is_more_than_a_fifth_from_the_price_change_is_refused(
    fresh_database_url: str,
) -> None:
    # 1:1 says the close halves; KARDA's fell to about a fifth.
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)

        result = ingest_cited_actions(conn, [karda_split(subject="Bonus 1:1")])

        assert stored(conn) == []
        (details,) = conn.execute(
            "SELECT details FROM ingest_runs WHERE source = 'cited_corporate_actions'"
        ).fetchone()  # type: ignore[misc]
    assert result.refused == 1
    (refusal,) = details["refused"]
    assert (refusal["symbol"], refusal["ex_date"], refusal["lines"]) == (
        "KARDA",
        "2020-07-02",
        [2],
    )
    assert "275.65" in refusal["reason"] and "57.90" in refusal["reason"]


def test_a_row_without_a_close_on_its_ex_date_or_before_it_is_refused(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)

        result = ingest_cited_actions(
            conn,
            [
                karda_split(ex_date=date(2020, 7, 4)),  # a Saturday: no Ex-Date close
                karda_split(ex_date=date(2020, 6, 30)),  # the first row: no previous close
            ],
        )

        assert stored(conn) == []
    assert result.refused == 2


def test_two_rows_on_one_ex_date_are_checked_against_the_price_together(
    fresh_database_url: str,
) -> None:
    gicl = "INE947T01014"
    with app_connection(fresh_database_url) as conn:
        eod(
            conn,
            [
                (date(2025, 10, 14), "GICL", "SM", gicl, "173.70"),
                (GICL_EX_DATE, "GICL", "SM", "INE947T01022", "46.50"),
            ],
        )

        result = ingest_cited_actions(
            conn,
            [
                CitedAction(
                    "GICL",
                    GICL_EX_DATE,
                    "Sub-Division from Rs. 10/- to Rs. 5/-",
                    GICL_CIRCULAR,
                    "split",
                    Fraction(1, 2),
                    2,
                ),
                CitedAction(
                    "GICL", GICL_EX_DATE, "Bonus 1:1", GICL_CIRCULAR, "bonus", Fraction(1, 2), 3
                ),
            ],
        )

    assert (result.rows_written, result.refused) == (2, 0)


def test_a_row_nse_lists_an_action_for_within_a_week_is_superseded_and_not_applied(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator, nse_list)"
            " VALUES ('KARDA', 'BE', '2020-07-03',"
            " 'Face Value Split From Rs 10 To Rs 2', 'split', 1, 5, 'equities')"
        )
        conn.commit()

        result = ingest_cited_actions(conn, [karda_split()])
        (before,) = queries.adjusted_daily_candles(
            conn, "KARDA", start=date(2020, 7, 1), end=date(2020, 7, 1), as_of=date(2020, 7, 3)
        )
        (details,) = conn.execute(
            "SELECT details FROM ingest_runs WHERE source = 'cited_corporate_actions'"
        ).fetchone()  # type: ignore[misc]

    assert (result.superseded, result.anomalies, result.refused) == (1, 0, 0)
    (superseded,) = details["superseded"]
    assert superseded["listed"] == [
        {"ex_date": "2020-07-03", "subject": "Face Value Split From Rs 10 To Rs 2"}
    ]
    assert before.factor == Fraction(1, 5)  # once, not 1/25


def test_a_listed_action_with_a_different_factor_beside_a_row_is_an_anomaly(
    fresh_database_url: str,
) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator, nse_list)"
            " VALUES ('KARDA', 'BE', '2020-07-02', 'Bonus 1:1', 'bonus', 1, 2, 'equities')"
        )
        conn.commit()

        result = ingest_cited_actions(conn, [karda_split()])
        (details,) = conn.execute(
            "SELECT details FROM ingest_runs WHERE source = 'cited_corporate_actions'"
        ).fetchone()  # type: ignore[misc]

    assert (result.superseded, result.anomalies) == (1, 1)
    (anomaly,) = details["anomalies"]
    assert (anomaly["cited_factor"], anomaly["listed_factor"]) == ("1/5", "1/2")


def test_a_row_reworded_after_it_was_stored_is_superseded_with_it_and_applied_never(
    fresh_database_url: str,
) -> None:
    # The table keeps the first wording: a reworded row is a second stored row on the Ex-Date.
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        ingest_cited_actions(conn, [karda_split()])
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator, nse_list)"
            " VALUES ('KARDA', 'BE', '2020-07-03',"
            " 'Face Value Split From Rs 10 To Rs 2', 'split', 1, 5, 'equities')"
        )
        conn.commit()

        reworded = ingest_cited_actions(
            conn, [karda_split(subject="Face Value Split (Sub-Division) From Rs 10 To Rs 2")]
        )
        (before,) = queries.adjusted_daily_candles(
            conn, "KARDA", start=date(2020, 7, 1), end=date(2020, 7, 1), as_of=date(2020, 7, 3)
        )

        assert len([row for row in stored(conn) if row[8] is not None]) == 2
    assert (reworded.rows_written, reworded.superseded, reworded.anomalies) == (1, 1, 0)
    assert before.factor == Fraction(1, 5)  # the listed split once; neither cited row


def listed(conn: psycopg.Connection, ex_date: date, subject: str) -> None:
    """NSE's main board list gives KARDA in BE one action, ingested as any day's would be."""
    action = nse_record("KARDA", subject, f"{ex_date:%d-%b-%Y}") | {"series": "BE"}
    nse = StubNse(json.dumps([action]).encode())
    ingest_corporate_actions(conn, nse, start=ex_date, end=ex_date)


def test_a_rebuild_reports_a_cited_action_nse_lists_later(fresh_database_url: str) -> None:
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        ingest_cited_actions(conn, [karda_split()])
        listed(conn, date(2020, 7, 3), "Face Value Split From Rs 10 To Rs 2")

        rebuilt = rebuild_instruments(conn)
        (error, details) = conn.execute(
            "SELECT error, details FROM ingest_runs WHERE source = 'instrument_links'"
        ).fetchone()  # type: ignore[misc]

    assert (rebuilt.superseded_cited_actions, rebuilt.cited_anomalies) == (1, 0)
    assert error is None
    assert details["superseded_cited_actions"] == {"agree": 1, "anomalies": 0}
    assert details["superseded_cited_action_records"] == [
        {
            "symbol": "KARDA",
            "ex_date": "2020-07-02",
            "subjects": [KARDA_SPLIT],
            "listed": [{"ex_date": "2020-07-03", "subject": "Face Value Split From Rs 10 To Rs 2"}],
            "cited_factor": "1/5",
            "listed_factor": "1/5",
        }
    ]


def test_a_rebuild_reports_a_cited_action_nse_lists_later_with_another_factor_as_an_anomaly(
    fresh_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The issue's test (#45): a Cited Action stored, a listed action for it ingested, one
    # rebuild, both reports.
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        ingest_cited_actions(conn, [karda_split()])
        listed(conn, KARDA_EX_DATE, "Bonus 1:1")

        with caplog.at_level(logging.WARNING, logger="tradeline.rebuild_instruments"):
            rebuilt = rebuild_instruments(conn)
        report = instruments_report(conn)

    assert (rebuilt.superseded_cited_actions, rebuilt.cited_anomalies) == (1, 1)
    (warning,) = [r for r in caplog.records if r.name == "tradeline.rebuild_instruments"]
    assert warning.getMessage() == "NSE's list prices a Cited Action's change differently"
    assert (warning.symbol, warning.cited_factor, warning.listed_factor) == ("KARDA", "1/5", "1/2")  # type: ignore[attr-defined]
    assert report.summary()["superseded_cited_actions"] == {"agree": 0, "anomalies": 1}
    section = report.text().split("Superseded Cited Actions: 1\n")[1].split("\n\n")[0]
    assert section.splitlines() == [
        "  Ex-Date  Symbol  cited factor  listed factor  listed actions",
        "  2020-07-02  KARDA  1/5  1/2  2020-07-02 Bonus 1:1  ANOMALY",
    ]


def test_a_rebuild_reports_a_cited_action_a_manual_link_brings_within_reach_of_nses_list(
    fresh_database_url: str,
) -> None:
    # NSE files the split under DHARAN, the name KARDA took after it; nothing joins the two
    # until the Operator cites the rename in a manual link.
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        ingest_cited_actions(conn, [karda_split()])
        eod(conn, [(date(2020, 7, 6), "DHARAN", "BE", KARDA_NEW, "58.50")])
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator, nse_list)"
            " VALUES ('DHARAN', 'BE', '2020-07-03',"
            " 'Face Value Split From Rs 10 To Rs 2', 'split', 1, 5, 'equities')"
        )
        conn.commit()

        apart = rebuild_instruments(conn)
        joined = rebuild_instruments(
            conn,
            manual_links=[ManualLink("KARDA", "DHARAN", date(2020, 7, 6), "NSE/CML/2020/99999")],
        )

    assert (apart.superseded_cited_actions, apart.cited_anomalies) == (0, 0)
    assert (joined.anomalies, joined.superseded_cited_actions, joined.cited_anomalies) == (0, 1, 0)


def test_a_rebuild_skips_and_warns_of_a_cited_action_whose_symbol_named_no_instrument_yet(
    fresh_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Inserted past the price check, which would refuse it: DHARAN's first row is on 6 July.
    with app_connection(fresh_database_url) as conn:
        karda_rows(conn)
        eod(conn, [(date(2020, 7, 6), "DHARAN", "BE", KARDA_NEW, "58.50")])
        conn.execute(
            "INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
            " factor_numerator, factor_denominator, nse_list, citation)"
            " VALUES ('DHARAN', 'BE', %s, %s, 'split', 1, 5, NULL, %s)",
            (KARDA_EX_DATE, KARDA_SPLIT, KARDA_CIRCULAR),
        )
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="tradeline.rebuild_instruments"):
            rebuilt = rebuild_instruments(conn)

    assert rebuilt.superseded_cited_actions == 0
    (warning,) = [
        r
        for r in caplog.records
        if r.getMessage() == "a Cited Action's Symbol named no Instrument by its Ex-Date"
    ]
    assert (warning.symbol, warning.ex_date) == ("DHARAN", "2020-07-02")  # type: ignore[attr-defined]
