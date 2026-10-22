"""``ingest cited-actions``: reviewed rows citing NSE's circulars, checked against the price.

The end-of-day rows are written out here with KARDA's real dates, ISINs, and
closes around its 2020 split (BE, the trade-for-trade series): 275.65 on the
last day before the Ex-Date, 57.90 on the Ex-Date, 2020-07-02, the old ISIN's
last row, and the new ISIN from 2020-07-03. GICL's split and bonus of
2025-10-15 (SM, 173.70 to 46.50) are two rows checked together.
"""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from fractions import Fraction

import psycopg

from tradeline import queries
from tradeline.adjust import interpret
from tradeline.cited_actions import CitedAction
from tradeline.db import app_connection
from tradeline.ingest.cited_actions import ingest_cited_actions
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
