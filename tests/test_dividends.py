"""Dividends credited in cash to a holder at the close before the Ex-Date (ADR 0020, #92).

The market is ``synthetic_eod.two_year_market`` with dividends listed for ASTER
while the Position from its 2022-08-03 pullback is held, backtested from
2022-07-01 to 2022-09-30 with ``pullback_v1`` at k = 2, r = 2 and n = 30, as in
``test_held_corporate_actions``: 111 shares filled at the open of 2022-08-04,
sold at the target, 697, during 2022-09-08. Dividends leave the path as it is.
Each subject, worked by hand:

- 2022-08-04, "Dividend - Rs 7 Per Share": the Position is bought at that
  morning's open, so it held nothing at the close before: nothing.
- 2022-08-10, "Interim Dividend - Rs 5 Per Share": 111 x 5 = Rs 555.
- 2022-08-17, "Dividend - 25%" with a stored face value of Rs 10: Rs 2.50 a
  share, 111 x 2.50 = Rs 277.50.
- 2022-08-24, "Final Dividend - Rs 3 Per Share/Special Dividend - Rs 1.50 Per
  Share": both, Rs 4.50 a share, 111 x 4.50 = Rs 499.50.
- 2022-08-30, "Dividend - Rs Per Share": no amount, so unparsed: nothing,
  listed with its reason.
- 2022-09-08, "Dividend - Re 1 Per Share": held at the close before and sold
  that day, so credited: Rs 111.

BIRCH's "Dividend - Rs 9 Per Share" on 2022-08-10 is ex-dated while nothing
holds it. In all, Rs 1,443 is credited.

Two Protocols are frozen over the same data, one crediting dividends and one
not (as ``pullback_v1``'s does not); each is backtested in its own account.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from synthetic_eod import (
    GRID,
    Dividend,
    freeze_protocol,
    load_market,
    protocol_terms,
    repo_calendar,
    two_year_market,
    write_fixtures,
)
from tradeline import queries
from tradeline.db import app_connection
from tradeline.dividends import dividend_check
from tradeline.engine import backtest
from tradeline.strategy.pullback import PullbackV1
from tradeline.tick_sizes import load_tick_sizes

D = Decimal
START, END = date(2022, 7, 1), date(2022, 9, 30)
BOUGHT, PER_SHARE, PERCENT, TWO, UNPARSED, SOLD = (
    date(2022, 8, 4),
    date(2022, 8, 10),
    date(2022, 8, 17),
    date(2022, 8, 24),
    date(2022, 8, 30),
    date(2022, 9, 8),
)
MARKET = two_year_market().with_events(
    "ASTER",
    Dividend(BOUGHT, "Dividend - Rs 7 Per Share"),
    Dividend(PER_SHARE, "Interim Dividend - Rs 5 Per Share"),
    Dividend(PERCENT, "Dividend - 25%", face_value=10),
    Dividend(TWO, "Final Dividend - Rs 3 Per Share/Special Dividend - Rs 1.50 Per Share"),
    Dividend(UNPARSED, "Dividend - Rs Per Share"),
    Dividend(SOLD, "Dividend - Re 1 Per Share"),
)
MARKET = MARKET.with_events("BIRCH", Dividend(PER_SHARE, "Dividend - Rs 9 Per Share"))
# The Protocols' grid holds the n = 30 the backtests run at.
TERMS = replace(protocol_terms(START, END), grid={**GRID, "n": (Decimal(30),)})


def _backtest(conn: psycopg.Connection, protocol: str) -> None:
    backtest(
        conn,
        PullbackV1(n=30),
        protocol=protocol,
        start=START,
        end=END,
        calendar=repo_calendar(),
        tick_sizes=load_tick_sizes(Path(__file__).parents[1] / "tick_sizes.csv"),
    )


@pytest.fixture(scope="module")
def backtested(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Each Protocol's hash, crediting (``on``) and not (``off``), each backtested."""
    fixtures = write_fixtures(MARKET, tmp_path_factory.mktemp("dividends"), repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        on = freeze_protocol(conn, MARKET, START, END, terms=replace(TERMS, dividends=True))
        off = freeze_protocol(conn, MARKET, START, END, terms=TERMS)
        _backtest(conn, on.hash)
        _backtest(conn, off.hash)
    return {"on": on.hash, "off": off.hash}


@pytest.fixture
def conn(database_url: str, backtested: dict[str, str]) -> Iterator[psycopg.Connection]:
    del backtested
    with app_connection(database_url) as conn:
        yield conn


def _account(conn: psycopg.Connection, protocol: str) -> queries.Account:
    (backtest_id,) = {
        r.backtest_id
        for r in queries.runs(conn, mode="backtest")
        if r.strategy.protocol == protocol
    }
    return {"mode": "backtest", "backtest_id": backtest_id, "dry_run": False}


def test_a_holder_at_the_close_before_the_ex_date_is_credited_each_form_of_subject(
    conn: psycopg.Connection, backtested: dict[str, str]
) -> None:
    credits = queries.dividend_credits(conn, **_account(conn, backtested["on"]))

    assert [(c.day, c.symbol, c.shares, c.per_share, c.cash, c.reason) for c in credits] == [
        (PER_SHARE, "ASTER", 111, D(5), D(555), None),
        (PERCENT, "ASTER", 111, D("2.5"), D("277.5"), None),
        (TWO, "ASTER", 111, D("4.5"), D("499.5"), None),
        (
            UNPARSED,
            "ASTER",
            111,
            None,
            None,
            "1 dividend and 0 amounts in 'Dividend - Rs Per Share'",
        ),
        (SOLD, "ASTER", 111, D(1), D(111), None),
    ]


def test_the_credit_is_cash_in_the_account_and_a_ledger_row_tagged_by_mode(
    conn: psycopg.Connection, backtested: dict[str, str]
) -> None:
    on, off = _account(conn, backtested["on"]), _account(conn, backtested["off"])
    cash_on, cash_off = (
        {
            r.as_of: r.cash or D(0)
            for r in queries.runs(conn, mode="backtest", backtest_id=account["backtest_id"])
        }
        for account in (on, off)
    )
    credits = queries.dividend_credits(conn, **on)

    # The one trade is the same in both accounts, so the cash differs by the credits alone.
    assert {
        day: cash_on[day] - cash_off[day]
        for day in (date(2022, 8, 8), PER_SHARE, PERCENT, TWO, UNPARSED, SOLD, END)
    } == {
        date(2022, 8, 8): D(0),
        PER_SHARE: D(555),
        PERCENT: D("832.5"),
        TWO: D("1332"),
        UNPARSED: D("1332"),
        SOLD: D("1443"),
        END: D("1443"),
    }
    assert {(c.mode, c.backtest_id) for c in credits} == {("backtest", on["backtest_id"])}


def test_with_the_flag_off_nothing_is_credited(
    conn: psycopg.Connection, backtested: dict[str, str]
) -> None:
    assert queries.dividend_credits(conn, **_account(conn, backtested["off"])) == []


def test_a_protocol_without_dividends_reads_back_to_its_hash_with_no_dividend_term(
    conn: psycopg.Connection, backtested: dict[str, str]
) -> None:
    """A Protocol frozen before dividends could be credited (``pullback_v1``'s) states no
    such term, so the one frozen with them off must state none either to keep its hash."""
    off = queries.protocol(conn, backtested["off"])
    on = queries.protocol(conn, backtested["on"])
    assert off is not None and on is not None

    assert (off.protocol.hash, off.protocol.dividends) == (backtested["off"], False)
    assert "dividends" not in off.protocol.content()
    assert (on.protocol.hash, on.protocol.dividends) == (backtested["on"], True)


def test_every_dividend_subject_is_parsed_or_listed_with_its_reason(
    conn: psycopg.Connection,
) -> None:
    check = dividend_check(conn, ["ASTER", "BIRCH", "CEDAR"], start=START, end=END)

    assert check.summary() == {
        "parsed": 6,
        "unparsed": 1,
        "unparsed_subjects": [
            {
                "instrument": "ASTER",
                "symbol": "ASTER",
                "ex_date": "2022-08-30",
                "subject": "Dividend - Rs Per Share",
                "reason": "1 dividend and 0 amounts in 'Dividend - Rs Per Share'",
            }
        ],
    }
