"""``compare-adjusted`` cuts its mismatched stretches at every Ex-Date.

A separate module so that its edited candles land in a database of their
own: RELIANCE's recorded bars with the closes of 16 and 19 August 2024 moved,
either side of its Rs 10 dividend ex 19 August.
"""

from datetime import date
from decimal import Decimal

from playback import shift_close, then
from test_compare_adjusted import END, FETCHED_ON, RELIANCE_TOKEN, START, load
from tradeline.compare_adjusted import compare_adjusted
from tradeline.db import app_connection


def test_mismatches_either_side_of_an_ex_date_are_two_stretches(database_url: str) -> None:
    edit = then(
        shift_close(RELIANCE_TOKEN, "2024-08-16", "5"),
        shift_close(RELIANCE_TOKEN, "2024-08-19", "5"),
    )
    with app_connection(database_url) as conn:
        load(conn, edit)
        report = compare_adjusted(
            conn, symbols=["RELIANCE"], start=START, end=END, as_of=FETCHED_ON
        )

    (reliance,) = report.symbols
    assert [(s.first, s.last, s.bars) for s in reliance.stretches] == [
        (date(2024, 8, 16), date(2024, 8, 16), 1),
        (date(2024, 8, 19), date(2024, 8, 19), 1),
    ]
    assert all(s.factor > Decimal(1) for s in reliance.stretches)
