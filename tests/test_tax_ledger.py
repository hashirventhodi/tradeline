"""The tax ledger as a pure function, with no database (ADR 0015 amendment of 2026-10-25).

Unless a test says otherwise the schedule is today's, frozen as of 2026-10-01:
short-term gains at 20 percent, long-term at 12.5 percent, each plus the 4
percent cess, so 20.8 and 13 percent; dividends at the configured 31.2
percent. Every lot below is a whole Position's realised sale, its basis and
proceeds after costs, and every expected rupee is worked by hand from those
rates.

The calendar is synthetic: every weekday trades except 2024-04-01, listed
as a holiday so the April debit has to look past it.
"""

from datetime import date
from decimal import Decimal

import pytest

from tradeline.tax import (
    DemergerCredit,
    Dividend,
    Losses,
    Sale,
    TaxRate,
    TaxSchedule,
    TaxTerms,
    TaxYear,
    liquidation_value,
    tax_ledger,
)
from tradeline.trading_calendar import CalendarDay, TradingCalendar

D = Decimal
CALENDAR = TradingCalendar(
    date(2015, 1, 1),
    date(2040, 12, 31),
    (CalendarDay(date(2024, 4, 1), "holiday", "synthetic", 1),),
)
# The rates in force from 2024-07-23 (Finance (No. 2) Act, 2024, sections 111A and
# 112A of the Income-tax Act, 1961) and the cess (Finance Act, 2018), with the rates
# before them so a schedule frozen earlier differs.
SCHEDULE = TaxSchedule(
    (
        TaxRate(date(2018, 4, 1), "short_term", D("0.15"), "s. 111A, Income-tax Act, 1961"),
        TaxRate(date(2018, 4, 1), "long_term", D("0.10"), "s. 112A, Income-tax Act, 1961"),
        TaxRate(date(2018, 4, 1), "cess", D("0.04"), "Finance Act, 2018"),
        TaxRate(date(2024, 7, 23), "short_term", D("0.20"), "Finance (No. 2) Act, 2024"),
        TaxRate(date(2024, 7, 23), "long_term", D("0.125"), "Finance (No. 2) Act, 2024"),
    )
)
TERMS = TaxTerms(SCHEDULE, rates_as_of=date(2026, 10, 1))
NONE = Losses(D("0"), D("0"))
# Bought and sold inside financial year 2025-26: short-term. Bought a year earlier: long-term.
SHORT = (date(2025, 5, 2), date(2025, 11, 3))
LONG = (date(2024, 5, 2), date(2025, 11, 3))


def sale(bought: date, sold: date, gain: str, *, basis: str = "100000") -> Sale:
    """A lot bought for ``basis`` and sold for ``basis`` plus ``gain``."""
    return Sale(bought=bought, sold=sold, basis=D(basis), proceeds=D(basis) + D(gain))


def taxes(*sales: Sale, terms: TaxTerms = TERMS) -> list[tuple[int, Decimal, date]]:
    """(financial year's first calendar year, net tax, its debit day) for each year."""
    ledger = tax_ledger(sales, terms=terms, calendar=CALENDAR)
    return [(y.year, y.tax, y.debit_day) for y in ledger.years]


# The twelve-month cut. A listed share is a short-term capital asset if held "not
# more than twelve months" immediately before its transfer: the Income-tax Act,
# 1961, s. 2(42A), first proviso (as amended from 2024-07-23), and from 2026-04-01
# the Income-tax Act, 2025, s. 2(101)(b). The day bought is not counted and the
# day sold is, so a share sold on the anniversary of its purchase has been held
# exactly twelve months: short-term. Long-term starts the day after.


def test_a_share_sold_the_day_before_its_anniversary_is_short_term() -> None:
    # 10,000 gain at 20.8 percent: 2,080, in financial year 2025-26, debited on
    # 2026-04-01 (a Wednesday).
    bought, sold = date(2024, 6, 14), date(2025, 6, 13)

    assert taxes(sale(bought, sold, "10000")) == [(2025, D("2080.00"), date(2026, 4, 1))]


def test_a_share_sold_on_its_anniversary_has_been_held_twelve_months_and_is_short_term() -> None:
    bought, sold = date(2024, 6, 14), date(2025, 6, 14)

    assert taxes(sale(bought, sold, "10000")) == [(2025, D("2080.00"), date(2026, 4, 1))]


def test_a_share_sold_the_day_after_its_anniversary_is_long_term() -> None:
    # 10,000 gain at 13 percent: 1,300.
    bought, sold = date(2024, 6, 14), date(2025, 6, 15)

    assert taxes(sale(bought, sold, "10000")) == [(2025, D("1300.00"), date(2026, 4, 1))]


# Set-off within a year: the Income-tax Act, 1961, s. 70(2) and (3); a short-term
# loss against either kind of gain, a long-term loss against long-term gains only.


def test_a_short_term_loss_is_set_off_against_a_long_term_gain() -> None:
    # 10,000 long-term less 4,000 short-term: 6,000 at 13 percent, 780.
    ledger = tax_ledger(
        [sale(*LONG, "10000"), sale(*SHORT, "-4000")], terms=TERMS, calendar=CALENDAR
    )

    (year,) = ledger.years
    assert (year.tax, year.carried_forward) == (D("780.00"), NONE)


def test_a_short_term_loss_is_set_off_against_short_term_gains_before_long_term() -> None:
    # 10,000 short-term less 4,000: 6,000 at 20.8 percent is 1,248; 5,000 long-term at
    # 13 percent is 650; 1,898 in all. Against the long-term gain it would be 1,950.
    sales = [sale(*SHORT, "10000"), sale(*SHORT, "-4000"), sale(*LONG, "5000")]

    assert taxes(*sales) == [(2025, D("1898.00"), date(2026, 4, 1))]


def test_a_long_term_loss_is_not_set_off_against_a_short_term_gain_and_is_carried() -> None:
    # 10,000 short-term at 20.8 percent: 2,080; the 4,000 long-term loss is carried.
    ledger = tax_ledger(
        [sale(*SHORT, "10000"), sale(*LONG, "-4000")], terms=TERMS, calendar=CALENDAR
    )

    (year,) = ledger.years
    assert (year.tax, year.carried_forward) == (D("2080.00"), Losses(D("0"), D("4000")))


def test_a_year_of_net_losses_owes_nothing_and_carries_both_kinds() -> None:
    # Short-term: 3,000 gain less 5,000 loss, 2,000 carried. Long-term: 1,000 loss carried.
    sales = [sale(*SHORT, "3000"), sale(*SHORT, "-5000"), sale(*LONG, "-1000")]
    ledger = tax_ledger(sales, terms=TERMS, calendar=CALENDAR)

    (year,) = ledger.years
    assert (year.tax, year.carried_forward) == (D("0.00"), Losses(D("2000"), D("1000")))


# Carry-forward: the Income-tax Act, 1961, s. 74(1) and (3). A loss unused in its
# year is carried for the eight financial years after it, a short-term loss against
# either kind of gain and a long-term loss against long-term only, and then lapses.


def in_year(year: int, gain: str, *, short_term: bool = True) -> Sale:
    """A lot sold on November 3 of financial year ``year``, short-term or long-term."""
    bought = date(year, 5, 4) if short_term else date(year - 1, 5, 4)
    return sale(bought, date(year, 11, 3), gain)


def year_of(ledger_years: tuple[TaxYear, ...], year: int) -> TaxYear:
    (found,) = (y for y in ledger_years if y.year == year)
    return found


def test_a_loss_is_set_off_in_the_eighth_year_after_it() -> None:
    # 2020-21's 5,000 short-term loss against 2028-29's 8,000: 3,000 at 20.8 percent, 624.
    ledger = tax_ledger(
        [in_year(2020, "-5000"), in_year(2028, "8000")], terms=TERMS, calendar=CALENDAR
    )

    assert [y.year for y in ledger.years] == list(range(2020, 2029))
    assert year_of(ledger.years, 2027).carried_forward == Losses(D("5000"), D("0"))
    final = year_of(ledger.years, 2028)
    assert (final.tax, final.debit_day, final.carried_forward) == (
        D("624.00"),
        date(2029, 4, 2),  # 2029-03-31 and 2029-04-01 are a weekend
        NONE,
    )


def test_a_loss_lapses_after_its_eighth_year_and_is_reported_as_expired() -> None:
    # 2029-30 is the ninth year: the 8,000 is taxed in full, 1,664.
    ledger = tax_ledger(
        [in_year(2020, "-5000"), in_year(2029, "8000")], terms=TERMS, calendar=CALENDAR
    )

    eighth, ninth = year_of(ledger.years, 2028), year_of(ledger.years, 2029)
    assert (eighth.carried_forward, eighth.expired) == (NONE, Losses(D("5000"), D("0")))
    assert (ninth.tax, ninth.expired) == (D("1664.00"), NONE)


def test_a_carried_short_term_loss_is_set_off_against_a_long_term_gain() -> None:
    # 10,000 long-term less 4,000 carried: 6,000 at 13 percent, 780.
    sales = [in_year(2020, "-4000"), in_year(2021, "10000", short_term=False)]

    assert taxes(*sales)[-1] == (2021, D("780.00"), date(2022, 4, 1))


def test_a_carried_long_term_loss_is_not_set_off_against_a_short_term_gain() -> None:
    sales = [in_year(2020, "-4000", short_term=False), in_year(2021, "10000")]
    ledger = tax_ledger(sales, terms=TERMS, calendar=CALENDAR)

    final = year_of(ledger.years, 2021)
    assert (final.tax, final.carried_forward) == (D("2080.00"), Losses(D("0"), D("4000")))


def test_the_oldest_carried_loss_is_set_off_first() -> None:
    # 2020-21's 3,000 and 2021-22's 2,000 against 2028-29's 4,000 gain: the 3,000 is
    # used whole and 1,000 of the 2,000, so 1,000 is carried and none taxed. In
    # 2029-30 the 2020-21 loss would have lapsed; the 1,000 left of 2021-22's is
    # set off against 2029-30's 1,000 gain.
    sales = [
        in_year(2020, "-3000"),
        in_year(2021, "-2000"),
        in_year(2028, "4000"),
        in_year(2029, "1000"),
    ]
    ledger = tax_ledger(sales, terms=TERMS, calendar=CALENDAR)

    eighth, ninth = year_of(ledger.years, 2028), year_of(ledger.years, 2029)
    assert (eighth.tax, eighth.carried_forward, eighth.expired) == (
        D("0.00"),
        Losses(D("1000"), D("0")),
        NONE,
    )
    assert (ninth.tax, ninth.carried_forward) == (D("0.00"), NONE)


# Dividends are income from other sources, taxed at the configured rate with cess
# already in it; a capital loss is never set off against them (s. 70 and 71(3)).


def test_a_dividend_is_taxed_at_the_default_rate() -> None:
    # 10,000 at 31.2 percent: 3,120.
    ledger = tax_ledger(
        [], [Dividend(date(2025, 8, 1), D("10000"))], terms=TERMS, calendar=CALENDAR
    )

    (year,) = ledger.years
    assert (year.year, year.dividends, year.tax) == (2025, D("10000"), D("3120.00"))


def test_a_dividend_is_taxed_at_the_configured_rate() -> None:
    # 10,000 at 20.8 percent: 2,080.
    terms = TaxTerms(SCHEDULE, rates_as_of=date(2026, 10, 1), dividend_rate=D("0.208"))
    ledger = tax_ledger(
        [], [Dividend(date(2025, 8, 1), D("10000"))], terms=terms, calendar=CALENDAR
    )

    assert [y.tax for y in ledger.years] == [D("2080.00")]


def test_a_capital_loss_is_not_set_off_against_a_dividend() -> None:
    ledger = tax_ledger(
        [sale(*SHORT, "-5000")],
        [Dividend(date(2025, 8, 1), D("10000"))],
        terms=TERMS,
        calendar=CALENDAR,
    )

    (year,) = ledger.years
    assert (year.tax, year.carried_forward) == (D("3120.00"), Losses(D("5000"), D("0")))


# A Demerger Credit is a sale of the child's shares on its first listing day against
# the child's share of the parent's basis. The child's shares are held since the
# parent's were: the Income-tax Act, 1961, s. 2(42A), Explanation 1(i)(g).


def credit(parent_bought: date, *, basis: str = "13510") -> DemergerCredit:
    # A parent lot of 1,00,000 whose announced split gives the child 13.51 percent, as
    # ITC Hotels' did; the child is first listed on 2025-01-29 and credited 15,000.
    return DemergerCredit(
        day=date(2025, 1, 29), parent_bought=parent_bought, amount=D("15000"), basis=D(basis)
    )


def test_a_demerger_credit_is_taxed_as_a_short_term_sale_against_its_share_of_the_basis() -> None:
    # 15,000 less 13,510 is 1,490, held since 2024-06-03: at 20.8 percent, 309.92.
    ledger = tax_ledger([], credits=[credit(date(2024, 6, 3))], terms=TERMS, calendar=CALENDAR)

    assert [(y.year, y.short_term_gain, y.tax) for y in ledger.years] == [
        (2024, D("1490"), D("309.92"))
    ]


def test_a_demerger_credit_counts_the_parents_holding_period() -> None:
    # The parent held since 2023-06-03: long-term, 1,490 at 13 percent, 193.70.
    ledger = tax_ledger([], credits=[credit(date(2023, 6, 3))], terms=TERMS, calendar=CALENDAR)

    assert [y.tax for y in ledger.years] == [D("193.70")]


def test_a_demerger_credit_with_no_announced_basis_is_taxed_in_full() -> None:
    # 15,000 at 20.8 percent: 3,120.
    ledger = tax_ledger(
        [], credits=[credit(date(2024, 6, 3), basis="0")], terms=TERMS, calendar=CALENDAR
    )

    assert [y.tax for y in ledger.years] == [D("3120.00")]


# Each year's net tax is debited on the first trading day after its March 31.


def test_the_debit_is_on_the_first_trading_day_after_march_31() -> None:
    # 2023-04-01 is a Saturday; 2024-04-01 is a Monday but a holiday in this calendar.
    sales = [in_year(2022, "10000"), in_year(2023, "10000")]

    assert [
        (y.year, y.debit_day) for y in tax_ledger(sales, terms=TERMS, calendar=CALENDAR).years
    ] == [
        (2022, date(2023, 4, 3)),
        (2023, date(2024, 4, 2)),
    ]


def test_a_sale_on_march_31_falls_in_the_year_ending_that_day() -> None:
    ledger = tax_ledger(
        [sale(date(2025, 10, 1), date(2026, 3, 31), "10000")], terms=TERMS, calendar=CALENDAR
    )

    assert [(y.year, y.debit_day) for y in ledger.years] == [(2025, date(2026, 4, 1))]


def test_the_cess_is_charged_on_the_tax() -> None:
    # 10,000 short-term at 20 percent is 2,000; a 4 percent cess adds 80, and a cess of 0
    # (a schedule without it) adds nothing.
    no_cess = TaxSchedule((*SCHEDULE.rows, TaxRate(date(2026, 1, 1), "cess", D("0"), "synthetic")))

    assert taxes(sale(*SHORT, "10000"))[0][1] == D("2080.00")
    assert taxes(sale(*SHORT, "10000"), terms=TaxTerms(no_cess, date(2026, 10, 1)))[0][1] == D(
        "2000.00"
    )


# The schedule is applied as of one date, the Protocol's freeze, to every trade.


def test_a_later_dated_schedule_row_takes_effect_on_its_date() -> None:
    # As of 2024-07-22: 10,000 short-term at 15 percent plus cess is 1,560, long-term at
    # 10 percent 1,040. As of 2024-07-23 the new rows give 2,080 and 1,300.
    short, long = sale(*SHORT, "10000"), sale(*LONG, "10000")
    before, on = TaxTerms(SCHEDULE, date(2024, 7, 22)), TaxTerms(SCHEDULE, date(2024, 7, 23))

    assert (taxes(short, terms=before)[0][1], taxes(long, terms=before)[0][1]) == (
        D("1560.00"),
        D("1040.00"),
    )
    assert (taxes(short, terms=on)[0][1], taxes(long, terms=on)[0][1]) == (
        D("2080.00"),
        D("1300.00"),
    )


def test_the_schedule_as_of_the_freeze_applies_to_a_trade_made_before_a_row_took_effect() -> None:
    # Sold in 2023, when the rate was 15 percent; frozen in 2026, so taxed at 20.8: 2,080.
    assert taxes(in_year(2023, "10000")) == [(2023, D("2080.00"), date(2024, 4, 2))]


def test_a_schedule_missing_an_item_as_of_its_date_is_refused() -> None:
    with pytest.raises(ValueError, match="no short_term, long_term, cess rate on 2017-01-01"):
        taxes(sale(*SHORT, "10000"), terms=TaxTerms(SCHEDULE, date(2017, 1, 1)))


# Liquidation Value: cash after selling everything on a day and paying every tax owed
# to then. Liquidation settles the final partial year on the day itself, and losses
# still unused are dropped and their size returned for the report.


def test_liquidation_value_sells_the_holdings_and_settles_the_partial_year() -> None:
    # 2025-26's 10,000 short-term gain was taxed 2,080 and debited on 2026-04-01, before
    # the day, so it is already out of the 50,000 cash. In 2026-27 a 10,000 short-term
    # gain realised on 2026-06-01 and the holding bought 2026-05-04 for 1,00,000, sold
    # for 1,05,000 on 2026-09-30: 15,000 at 20.8 percent, 3,120 due on the day.
    # 50,000 + 1,05,000 - 3,120 = 1,51,880.
    day = date(2026, 9, 30)
    realised = [in_year(2025, "10000"), sale(date(2026, 5, 4), date(2026, 6, 1), "10000")]
    holding = sale(date(2026, 5, 4), day, "5000")

    value = liquidation_value(
        day, cash=D("50000"), holdings=[holding], sales=realised, terms=TERMS, calendar=CALENDAR
    )

    assert (value.value, value.tax_due, value.losses_dropped) == (
        D("151880.00"),
        D("3120.00"),
        NONE,
    )
    assert [(y.year, y.tax, y.debit_day) for y in value.ledger.years] == [
        (2025, D("2080.00"), date(2026, 4, 1)),
        (2026, D("3120.00"), day),
    ]


def test_losses_unused_at_liquidation_are_dropped_and_their_size_returned() -> None:
    # 2025-26 carries a 2,000 long-term loss. In 2026-27 a 3,000 long-term gain is
    # realised and the holding is sold at an 8,000 short-term loss. The year's own loss
    # is set off first (s. 70, before s. 74's carried losses): 3,000 of it against the
    # gain, so no tax, and 5,000 short-term and the 2,000 long-term are dropped.
    # 1,00,000 cash + 92,000 for the holding.
    day = date(2026, 9, 30)
    realised = [
        in_year(2025, "-2000", short_term=False),
        sale(date(2025, 5, 4), date(2026, 6, 1), "3000"),
    ]
    holding = sale(date(2026, 5, 4), day, "-8000")

    value = liquidation_value(
        day, cash=D("100000"), holdings=[holding], sales=realised, terms=TERMS, calendar=CALENDAR
    )

    assert (value.value, value.tax_due, value.losses_dropped) == (
        D("192000.00"),
        D("0.00"),
        Losses(D("5000"), D("2000")),
    )


def test_a_long_term_loss_carried_to_liquidation_is_dropped() -> None:
    # A 2,000 long-term loss of 2025-26 and a 5,000 short-term gain at liquidation:
    # 1,040 due, and the long-term loss cannot be used, so 2,000 is dropped.
    day = date(2026, 9, 30)
    value = liquidation_value(
        day,
        cash=D("0"),
        holdings=[sale(date(2026, 5, 4), day, "5000")],
        sales=[in_year(2025, "-2000", short_term=False)],
        terms=TERMS,
        calendar=CALENDAR,
    )

    assert (value.value, value.tax_due, value.losses_dropped) == (
        D("103960.00"),
        D("1040.00"),
        Losses(D("0"), D("2000")),
    )


def test_liquidation_on_the_april_debit_day_owes_the_year_just_ended_too() -> None:
    # 2025-26's 2,080 is debited on 2026-04-01; cash is taken before that day's debit, so
    # liquidating that day owes it. Nothing is realised in 2026-27.
    day = date(2026, 4, 1)
    value = liquidation_value(
        day,
        cash=D("10000"),
        holdings=[],
        sales=[in_year(2025, "10000")],
        terms=TERMS,
        calendar=CALENDAR,
    )

    assert (value.value, value.tax_due) == (D("7920.00"), D("2080.00"))


def test_a_liquidation_with_an_event_after_its_day_is_refused() -> None:
    with pytest.raises(ValueError, match="2026-10-01 is after the liquidation on 2026-09-30"):
        liquidation_value(
            date(2026, 9, 30),
            cash=D("0"),
            holdings=[],
            sales=[sale(date(2026, 5, 4), date(2026, 10, 1), "1")],
            terms=TERMS,
            calendar=CALENDAR,
        )


def test_a_holding_not_sold_on_the_liquidation_day_is_refused() -> None:
    with pytest.raises(
        ValueError, match="sold on 2026-09-29, not on the liquidation day 2026-09-30"
    ):
        liquidation_value(
            date(2026, 9, 30),
            cash=D("0"),
            holdings=[sale(date(2026, 5, 4), date(2026, 9, 29), "1")],
            sales=[],
            terms=TERMS,
            calendar=CALENDAR,
        )


# The Benchmark is one lot, bought on an account's first day, held to its last, and
# taxed once on that sale by the same holding rule. Its basis includes the stamp duty
# on its units and its proceeds are after STT and any exit load.


def test_the_benchmark_held_about_twelve_months_is_taxed_as_short_term() -> None:
    # Bought 2025-10-01, sold on the anniversary 2026-10-01: twelve months, short-term.
    # A 12,000 gain at 20.8 percent is 2,496: 1,12,000 - 2,496 = 1,09,504.
    day = date(2026, 10, 1)
    value = liquidation_value(
        day,
        cash=D("0"),
        holdings=[sale(date(2025, 10, 1), day, "12000")],
        sales=[],
        terms=TERMS,
        calendar=CALENDAR,
    )

    assert (value.value, value.tax_due) == (D("109504.00"), D("2496.00"))
    assert [(y.year, y.short_term_gain, y.long_term_gain) for y in value.ledger.years] == [
        (2026, D("12000"), D("0"))
    ]


def test_the_benchmark_held_over_the_holdouts_two_years_is_taxed_as_long_term() -> None:
    # Bought 2024-10-01, sold 2026-09-30: a 12,000 gain at 13 percent is 1,560, and
    # nothing is due for the years before, which realised nothing.
    day = date(2026, 9, 30)
    value = liquidation_value(
        day,
        cash=D("0"),
        holdings=[sale(date(2024, 10, 1), day, "12000")],
        sales=[],
        terms=TERMS,
        calendar=CALENDAR,
    )

    assert (value.value, value.tax_due) == (D("110440.00"), D("1560.00"))
