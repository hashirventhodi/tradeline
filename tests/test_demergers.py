"""Demerger Credits: a holder at the close before the Ex-Date credited on the child's first
listing day (ADR 0020, #95).

The market is ``synthetic_eod.two_year_market`` with ASTERNEW, a child listed in
the BE series on 2022-08-22 at a close of 1,000.00 that never moves, and a
demerger table row: 1 ASTERNEW for 10 ASTER, Ex-Date 2022-08-10, basis
86.49 / 13.51. As in ``test_dividends``, ``pullback_v1`` at k = 2, r = 2,
n = 30 holds 111 ASTER from the open of 2022-08-04 to its target during
2022-09-08. Worked by hand:

- held at the close of 2022-08-09, so entitled to 111 / 10 = 11.1 ASTERNEW;
- credited on 2022-08-22, at its first close: 11.1 x 1,000.00 = Rs 11,100.00;
- the child's share of the Position's basis (its Fill's value and charges) is
  13.51 percent of it, and the Position keeps 86.49 percent;
- the credit is a sale of the child's shares, held since 2022-08-04, so a
  short-term gain of 11,100 less that share in the year from 2022-04-01.

ASTER also has a rights issue ex-dated 2022-08-17, opening 5.00 under the close
before (and gapping back up the next day, so the trade is unchanged): the
Position, held at the close before, is listed with that gap. BIRCH's rights
issue of the same day is held by nothing.

Two Protocols are frozen over the same data, one crediting demergers and one not
(as ``pullback_v1``'s does not); each is backtested in its own account.
"""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from synthetic_eod import (
    GRID,
    Absent,
    Gap,
    Rights,
    Stock,
    freeze_protocol,
    load_market,
    protocol_terms,
    repo_calendar,
    two_year_market,
    write_fixtures,
)
from test_tax_ledger import TERMS as TAX_TERMS
from tradeline import queries
from tradeline.db import app_connection
from tradeline.demergers import load_demergers
from tradeline.engine import backtest
from tradeline.freeze import SnapshotChanged
from tradeline.ingest.demergers import ingest_demergers
from tradeline.rights import rights_held
from tradeline.strategy.pullback import PullbackV1
from tradeline.tax import tax_ledger
from tradeline.tick_sizes import load_tick_sizes

D = Decimal
START, END = date(2022, 7, 1), date(2022, 9, 30)
BOUGHT, EX_DATE, LISTED, SOLD = (
    date(2022, 8, 4),
    date(2022, 8, 10),
    date(2022, 8, 22),
    date(2022, 9, 8),
)
RIGHTS = date(2022, 8, 17)
_BASE = two_year_market()
MARKET = replace(
    _BASE,
    stocks=(
        *_BASE.stocks,
        Stock(
            "ASTERNEW",
            "INE000N01016",
            start=D("1000.00"),
            slope=D("0"),
            half_range=D("1.00"),
            volume=100_000,
            listed=LISTED,
            series="BE",
            events=(Absent(_BASE.start, date(2022, 8, 21)),),
        ),
    ),
).with_events(
    "ASTER",
    Rights(RIGHTS, "Rights 1:5 @ Premium Rs 300/-"),
    Gap(RIGHTS, D("-5.00")),
    Gap(date(2022, 8, 18), D("5.00")),
)
MARKET = MARKET.with_events("BIRCH", Rights(RIGHTS, "Rights 1:4 @ Premium Rs 200/-"))
HEADER = "parent,child,ratio,ex_date,listed_on,parent_basis,child_basis,citation\n"
ROW = "ASTER,ASTERNEW,1:10,2022-08-10,2022-08-22,86.49,13.51,NSE/CMTR/65845\n"
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


def _table(directory: Path, row: str = ROW) -> Path:
    path = directory / "demergers.csv"
    path.write_text(HEADER + row)
    return path


@pytest.fixture(scope="module")
def loaded(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Each Protocol's hash, crediting (``on``) and not (``off``), each backtested."""
    directory = tmp_path_factory.mktemp("demergers")
    fixtures = write_fixtures(MARKET, directory, repo_calendar())
    with app_connection(database_url) as conn:
        load_market(conn, fixtures, MARKET)
        ingest_demergers(conn, load_demergers(_table(directory)))
        on = freeze_protocol(conn, MARKET, START, END, terms=replace(TERMS, demergers=True))
        off = freeze_protocol(conn, MARKET, START, END, terms=TERMS)
        _backtest(conn, on.hash)
        _backtest(conn, off.hash)
    return {"on": on.hash, "off": off.hash}


@pytest.fixture
def conn(database_url: str, loaded: dict[str, str]) -> Iterator[psycopg.Connection]:
    del loaded
    with app_connection(database_url) as conn:
        yield conn


def _account(conn: psycopg.Connection, protocol: str) -> queries.Account:
    (backtest_id,) = {
        r.backtest_id
        for r in queries.runs(conn, mode="backtest")
        if r.strategy.protocol == protocol
    }
    return {"mode": "backtest", "backtest_id": backtest_id, "dry_run": False}


def _cost(conn: psycopg.Connection, account: queries.Account) -> Decimal:
    """What the ASTER Position cost: its buy Fill's value and charges."""
    (fill,) = [f for f in queries.fills(conn, **account) if f.side == "buy"]
    assert fill.symbol == "ASTER" and fill.day == BOUGHT and fill.charges is not None
    return fill.price * fill.shares + fill.charges.total


def test_a_holder_is_credited_on_the_childs_first_listing_day_at_its_first_close(
    conn: psycopg.Connection, loaded: dict[str, str]
) -> None:
    account = _account(conn, loaded["on"])
    cost = _cost(conn, account)

    (credit,) = queries.demerger_credits(conn, **account)

    assert (
        credit.day,
        credit.parent,
        credit.child,
        credit.ex_date,
        credit.shares,
        credit.close,
        credit.cash,
        credit.bought,
    ) == (LISTED, "ASTER", "ASTERNEW", EX_DATE, 111, D("1000.00"), D("11100.00"), BOUGHT)
    assert credit.basis == (cost * D("0.1351")).quantize(D("0.01"))


def test_the_parents_basis_is_reduced_by_the_childs_share(
    conn: psycopg.Connection, loaded: dict[str, str]
) -> None:
    on, off = _account(conn, loaded["on"]), _account(conn, loaded["off"])
    cost = _cost(conn, on)
    (position,) = queries.positions(conn, **on)

    assert queries.position_bases(conn, **on) == {
        position.id: cost - (cost * D("0.1351")).quantize(D("0.01"))
    }
    (unreduced,) = queries.positions(conn, **off)
    assert queries.position_bases(conn, **off) == {unreduced.id: _cost(conn, off)}


def test_the_credit_is_taxed_as_a_sale_against_the_childs_basis(
    conn: psycopg.Connection, loaded: dict[str, str]
) -> None:
    (credit,) = queries.demerger_credits(conn, **_account(conn, loaded["on"]))

    ledger = tax_ledger(
        [], credits=[credit.tax_credit()], terms=TAX_TERMS, calendar=repo_calendar()
    )

    (year,) = ledger.years
    assert (year.year, year.short_term_gain, year.long_term_gain) == (
        2022,
        D("11100.00") - credit.basis,
        D(0),
    )


def test_the_credit_is_cash_in_the_account_from_the_listing_day(
    conn: psycopg.Connection, loaded: dict[str, str]
) -> None:
    on, off = _account(conn, loaded["on"]), _account(conn, loaded["off"])
    cash_on, cash_off = (
        {
            r.as_of: r.cash or D(0)
            for r in queries.runs(conn, mode="backtest", backtest_id=account["backtest_id"])
        }
        for account in (on, off)
    )

    # The one trade is the same in both accounts, so the cash differs by the credit alone.
    assert {
        day: cash_on[day] - cash_off[day] for day in (EX_DATE, date(2022, 8, 19), LISTED, SOLD, END)
    } == {EX_DATE: D(0), date(2022, 8, 19): D(0), LISTED: D(11100), SOLD: D(11100), END: D(11100)}


def test_with_the_flag_off_nothing_is_credited_and_the_protocol_states_no_demergers(
    conn: psycopg.Connection, loaded: dict[str, str]
) -> None:
    off = queries.protocol(conn, loaded["off"])
    on = queries.protocol(conn, loaded["on"])
    assert off is not None and on is not None

    assert queries.demerger_credits(conn, **_account(conn, loaded["off"])) == []
    assert (off.protocol.hash, off.protocol.demergers) == (loaded["off"], False)
    assert "demergers" not in off.protocol.content()
    assert "demergers" not in off.protocol.content()["data_snapshot"]
    assert (on.protocol.hash, on.protocol.demergers) == (loaded["on"], True)


def test_the_tables_hash_is_in_the_snapshot_and_an_edit_stored_after_the_freeze_refuses(
    database_url: str,
    loaded: dict[str, str],
    clone_database: Callable[[str], str],
    tmp_path: Path,
) -> None:
    edited = load_demergers(_table(tmp_path, ROW.replace("1:10", "1:5")))
    copy = clone_database(database_url)
    with app_connection(copy) as conn:
        frozen = queries.protocol(conn, loaded["on"])
        assert frozen is not None
        assert frozen.protocol.snapshot.demergers == load_demergers(_table(tmp_path)).hash

        ingest_demergers(conn, edited)

        with pytest.raises(SnapshotChanged, match="the demerger table"):
            _backtest(conn, loaded["on"])
        # A Protocol not crediting demergers never reads the table.
        _backtest(conn, loaded["off"])


def test_each_position_held_through_a_rights_issue_is_listed_with_its_ex_date_gap(
    conn: psycopg.Connection, loaded: dict[str, str]
) -> None:
    account = _account(conn, loaded["off"])
    (position,) = queries.positions(conn, **account)
    (before,) = queries.end_of_day(conn, "ASTER", start=date(2022, 8, 16), end=date(2022, 8, 16))
    assert before.close is not None

    (held,) = rights_held(conn, calendar=repo_calendar(), **account)

    assert (held.position_id, held.symbol, held.ex_date, held.subject, held.shares) == (
        position.id,
        "ASTER",
        RIGHTS,
        "Rights 1:5 @ Premium Rs 300/-",
        111,
    )
    assert (held.previous_close, held.open, held.gap) == (
        before.close,
        before.close - D(5),
        D(-5) / before.close,
    )
