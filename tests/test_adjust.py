"""The pure adjustment function and the reading of NSE's corporate-action subjects.

Hand-computed expectations throughout. Prices are rounded to the exchange tick
of 5 paisa with ties to the even tick, the rule that reproduces Kite's
adjusted RELIANCE bars exactly (see ``tradeline.adjust``); the ties below are
chosen so that the rule is observable.
"""

from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from tradeline.adjust import (
    AdjustedBar,
    CorporateAction,
    Kind,
    RawBar,
    adjust,
    interpret,
    superseded,
)

D = Decimal


def bar(day: date, o: str, h: str, lo: str, c: str, volume: int) -> RawBar:
    return RawBar(
        symbol="X",
        series="EQ",
        trade_date=day,
        open=D(o),
        high=D(h),
        low=D(lo),
        close=D(c),
        volume=volume,
    )


def action(
    ex_date: date, kind: Kind, factor: Fraction | None = None, subject: str = ""
) -> CorporateAction:
    return CorporateAction(
        symbol="X", series="EQ", ex_date=ex_date, subject=subject, kind=kind, factor=factor
    )


JUL_1, JUL_2, JUL_3, JUL_5, JUL_8 = (
    date(2024, 7, 1),
    date(2024, 7, 2),
    date(2024, 7, 3),
    date(2024, 7, 5),
    date(2024, 7, 8),
)


def test_a_one_for_ten_split_divides_prices_by_ten_and_multiplies_volume_before_the_ex_date() -> (
    None
):
    bars = [
        bar(JUL_1, "1234.50", "1250.00", "1230.05", "1245.55", 1000),
        bar(JUL_3, "124.00", "126.00", "123.00", "125.00", 12000),
    ]
    split = action(JUL_3, "split", Fraction(1, 10), "Face Value Split From Rs 10 To Re 1")

    before, on = adjust(bars, [split], as_of=JUL_8)

    assert before == AdjustedBar(
        symbol="X",
        series="EQ",
        trade_date=JUL_1,
        open=D("123.45"),
        high=D("125.00"),
        low=D("123.00"),  # 123.005 is 2460.1 ticks: rounds down
        close=D("124.55"),  # 124.555 is 2491.1 ticks: rounds down
        volume=10000,
        factor=Fraction(1, 10),
        unadjusted=(),
    )
    assert on == AdjustedBar(
        symbol="X",
        series="EQ",
        trade_date=JUL_3,
        open=D("124.00"),
        high=D("126.00"),
        low=D("123.00"),
        close=D("125.00"),
        volume=12000,
        factor=Fraction(1),
        unadjusted=(),
    )


def test_a_one_for_one_bonus_halves_prices_to_the_even_tick_and_doubles_volume() -> None:
    # RELIANCE on 1 July 2024 as NSE states it; Kite shows 1562.5 / 1579.4 / 1555.7 / 1560.15.
    bars = [bar(JUL_1, "3125.05", "3158.80", "3111.35", "3120.30", 3_862_153)]
    bonus = action(date(2024, 10, 28), "bonus", Fraction(1, 2), "Bonus 1:1")

    (adjusted,) = adjust(bars, [bonus], as_of=date(2024, 10, 28))

    assert (adjusted.open, adjusted.high, adjusted.low, adjusted.close) == (
        D("1562.50"),  # 1562.525: a tie, 31250.5 ticks, to the even tick 31250
        D("1579.40"),
        D("1555.70"),  # 1555.675: a tie, 31113.5 ticks, to the even tick 31114
        D("1560.15"),
    )
    assert adjusted.volume == 7_724_306
    assert adjusted.factor == Fraction(1, 2)


def test_a_bonus_whose_factor_is_not_a_terminating_decimal_still_rounds_ties_exactly() -> None:
    """Bonus 1:5 is a factor of 5/6; a Decimal approximation would push every tie down."""
    bars = [bar(JUL_1, "1500.03", "1500.09", "1500.03", "1500.09", 3)]
    bonus = action(JUL_3, "bonus", Fraction(5, 6), "Bonus 1:5")

    (adjusted,) = adjust(bars, [bonus], as_of=JUL_3)

    assert adjusted.open == D("1250.00")  # 1250.025 = 25000.5 ticks -> 25000
    assert adjusted.high == D("1250.10")  # 1250.075 = 25001.5 ticks -> 25002
    assert adjusted.volume == 4  # 3.6 shares


def test_two_actions_in_one_history_compound_and_each_bar_carries_its_own_factor() -> None:
    bars = [
        bar(JUL_1, "1000.00", "1000.00", "1000.00", "1000.00", 100),
        bar(JUL_3, "500.00", "500.00", "500.00", "500.00", 200),
        bar(JUL_5, "100.00", "100.00", "100.00", "100.00", 1000),
    ]
    bonus = action(JUL_3, "bonus", Fraction(1, 2), "Bonus 1:1")
    split = action(JUL_5, "split", Fraction(1, 5), "Face Value Split From Rs 10 To Rs 2")

    first, second, third = adjust(bars, [split, bonus], as_of=JUL_8)

    assert (first.close, first.volume, first.factor) == (D("100.00"), 1000, Fraction(1, 10))
    assert (second.close, second.volume, second.factor) == (D("100.00"), 1000, Fraction(1, 5))
    assert (third.close, third.volume, third.factor) == (D("100.00"), 1000, Fraction(1))


def test_an_action_on_a_day_with_no_bar_adjusts_everything_before_it_and_nothing_after() -> None:
    saturday = date(2024, 7, 6)
    bars = [
        bar(JUL_5, "200.00", "200.00", "200.00", "200.00", 10),
        bar(JUL_8, "100.00", "100.00", "100.00", "100.00", 20),
    ]
    bonus = action(saturday, "bonus", Fraction(1, 2), "Bonus 1:1")

    friday, monday = adjust(bars, [bonus], as_of=JUL_8)

    assert (friday.close, friday.volume) == (D("100.00"), 20)
    assert (monday.close, monday.volume) == (D("100.00"), 20)


def test_a_rights_issue_is_flagged_on_earlier_bars_and_leaves_prices_alone() -> None:
    bars = [
        bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10),
        bar(JUL_3, "90.00", "90.00", "90.00", "90.00", 10),
    ]
    rights = action(JUL_3, "rights", None, "Rights 1:4 @ Premium Rs 20/-")

    before, on = adjust(bars, [rights], as_of=JUL_8)

    assert (before.close, before.volume, before.factor) == (D("100.00"), 10, Fraction(1))
    assert before.unadjusted == (rights,)
    assert on.unadjusted == ()


def test_a_dividend_is_flagged_but_a_meeting_is_not() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    dividend = action(JUL_2, "dividend", None, "Dividend - Rs 8 Per Share")
    meeting = action(JUL_3, "other", None, "Annual General Meeting")

    (adjusted,) = adjust(bars, [meeting, dividend], as_of=JUL_8)

    assert adjusted.unadjusted == (dividend,)
    assert adjusted.close == D("100.00")


def test_an_action_dated_after_the_as_of_date_is_neither_applied_nor_flagged() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    bonus = action(JUL_3, "bonus", Fraction(1, 2), "Bonus 1:1")
    rights = action(JUL_5, "rights", None, "Rights 1:1 @ Premium Rs 0/-")

    (as_of_day_before,) = adjust(bars, [bonus, rights], as_of=JUL_2)
    (as_of_ex_date,) = adjust(bars, [bonus, rights], as_of=JUL_3)

    assert (as_of_day_before.close, as_of_day_before.factor) == (D("100.00"), Fraction(1))
    assert as_of_day_before.unadjusted == ()
    assert (as_of_ex_date.close, as_of_ex_date.factor) == (D("50.00"), Fraction(1, 2))
    assert as_of_ex_date.unadjusted == ()


def test_a_missing_price_stays_missing_and_bars_come_back_in_the_order_given() -> None:
    bars = [
        RawBar(
            "X", "EQ", trade_date=JUL_3, open=None, high=None, low=None, close=D("50.00"), volume=0
        ),
        bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10),
    ]
    bonus = action(JUL_3, "bonus", Fraction(1, 2), "Bonus 1:1")

    later, earlier = adjust(bars, [bonus], as_of=JUL_8)

    assert (later.trade_date, later.open, later.close) == (JUL_3, None, D("50.00"))
    assert (earlier.trade_date, earlier.close) == (JUL_1, D("50.00"))


def test_an_action_listed_under_two_series_or_with_a_revised_subject_is_applied_once() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    bonus = action(JUL_3, "bonus", Fraction(1, 2), "Bonus 1:1")
    under_be = CorporateAction(
        symbol="X",
        series="BE",
        ex_date=JUL_3,
        subject="Bonus 1:1",
        kind="bonus",
        factor=Fraction(1, 2),
    )
    revised = action(JUL_3, "bonus", Fraction(1, 2), "Bonus 1:1 (Revised)")
    dividend = action(JUL_3, "dividend", None, "Dividend - Rs 8 Per Share")
    dividend_under_be = CorporateAction(
        symbol="X",
        series="BE",
        ex_date=JUL_3,
        subject="Dividend - Rs 8 Per Share",
        kind="dividend",
        factor=None,
    )

    (adjusted,) = adjust(bars, [bonus, under_be, revised, dividend, dividend_under_be], as_of=JUL_8)

    assert (adjusted.close, adjusted.factor) == (D("50.00"), Fraction(1, 2))
    assert adjusted.unadjusted == (dividend,)


def test_an_adjusting_action_without_a_factor_is_refused() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]

    with pytest.raises(ValueError, match="no factor"):
        adjust(bars, [action(JUL_3, "bonus", None, "Bonus 1:1")], as_of=JUL_8)


@pytest.mark.parametrize(
    ("subject", "kind", "factor"),
    [
        ("Bonus 1:1", "bonus", Fraction(1, 2)),
        ("Bonus Ratio 1:1", "bonus", Fraction(1, 2)),
        (" Bonus 1:2", "bonus", Fraction(2, 3)),
        ("Bonus 4:1", "bonus", Fraction(1, 5)),
        ("Bonus- 1:1", "bonus", Fraction(1, 2)),
        ("Bonus 1: 25", "bonus", Fraction(25, 26)),
        # TOUCHWOOD, SM, ex 2019-08-22: 1.5 new shares for every 1 held, so 3:2.
        ("BONUS 1.5:1", "bonus", Fraction(2, 5)),
        ("Bonus 1:1 /Dividend- Rs 29 Per Share", "bonus", Fraction(1, 2)),
        (
            "Annual General Meeting/Dividend - Rs 20 Per Share/Bonus 1:1 (Revised)",
            "bonus",
            Fraction(1, 2),
        ),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
            "split",
            Fraction(1, 10),
        ),
        (
            "Face Value Split (Sub-Division) - From Rs10/- Per Share To Rs 5/- Per Share",
            "split",
            Fraction(1, 2),
        ),
        ("Face Value Split From Rs 2 To Re 1", "split", Fraction(1, 2)),
        # GRANULES, ex 2015-03-23: NSE's subject has no "From".
        ("Face Value Split Rs 10 To Rs 1", "split", Fraction(1, 10)),
        # JSWSTEEL, ex 2017-01-04, and KARURVYSYA, ex 2016-11-17: NSE's 2016 to 2018 short form.
        ("Fv Splt Frm Rs 10 To Re 1", "split", Fraction(1, 10)),
        ("Fv Splt Frm Rs 10 To Rs 2", "split", Fraction(1, 5)),
        (
            "Interim Div - Rs 2/- Per Share + Face Value Split (Sub-Division)"
            " - From Rs 10/- Per Share To Re 1/- Per Share",
            "split",
            Fraction(1, 10),
        ),
        # A reverse split is recorded as a split whose factor is above one.
        (
            "Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share",
            "split",
            Fraction(10),
        ),
        # Both in one subject: the factors compound.
        (
            "Bonus 1:1/Face Value Split (Sub-Division)"
            " - From Rs 10/- Per Share To Rs 2/- Per Share",
            "split",
            Fraction(1, 10),
        ),
        ("Rights 1:4 @ Premium Rs 1247/-", "rights", None),
        ("Rights 100:224 @ Premium Rs 20/-", "rights", None),
        ("Rights Issue 1:3@ Premium Rs 10/-", "rights", None),
        ("Dividend - Re 0.5/- Per Share/ Right 1:4", "rights", None),
        ("Dividend - Rs 10 Per Share", "dividend", None),
        ("Annual General Meeting/Dividend - Rs 20 Per Share", "dividend", None),
        ("Int Div-Rs.11/- Per Sh   Purpose Revised", "dividend", None),
        ("Interim Dividend - Rs 21 Per Share", "dividend", None),
        (
            "Distribution - Rs 1.92 Per Unit Consisting Of Interest Payment - Rs 1.30 Per Unit",
            "dividend",
            None,
        ),
        ("Annual General Meeting", "other", None),
        ("Buy Back", "other", None),
        ("Demerger", "other", None),
        # Bonus debentures and preference shares are not equity bonuses.
        ("Scheme Of Arrangement - Bonus Ncrps 1:1", "other", None),
        ("Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held", "other", None),
        ("Bonus 1:1 Ncrps", "other", None),
        # A consolidation without a From/To pair cannot be given a factor.
        ("Capital Reduction Rs 10 To Rs 5 / Consolidation Rs 5 To Rs10", "other", None),
    ],
)
def test_nse_subjects_are_read_into_a_kind_and_a_factor(
    subject: str, kind: str, factor: Fraction | None
) -> None:
    assert interpret(subject) == (kind, factor)


def test_the_ex_date_is_the_boundary_for_the_bar_and_for_the_as_of_date_alike() -> None:
    """The bar before the ex-date is adjusted, the ex-date bar is not; and the action is
    unknown on the day before its ex-date, known on the ex-date itself."""
    bars = [
        bar(JUL_2, "100.00", "100.00", "100.00", "100.00", 10),
        bar(JUL_3, "50.00", "50.00", "50.00", "50.00", 20),
    ]
    bonus = action(JUL_3, "bonus", Fraction(1, 2), "Bonus 1:1")

    day_before, ex_day = adjust(bars, [bonus], as_of=JUL_3)
    unknown_before, unknown_ex = adjust(bars, [bonus], as_of=JUL_2)

    assert (day_before.close, day_before.volume, day_before.factor) == (
        D("50.00"),
        20,
        Fraction(1, 2),
    )
    assert (ex_day.close, ex_day.volume, ex_day.factor) == (D("50.00"), 20, Fraction(1))
    assert (unknown_before.close, unknown_before.factor) == (D("100.00"), Fraction(1))
    assert (unknown_ex.close, unknown_ex.factor) == (D("50.00"), Fraction(1))


CIRCULAR = "NSE/CML/44773; https://nsearchives.nseindia.com/content/circulars/CML44835.pdf"


def cited(ex_date: date, factor: Fraction, subject: str = "Sub-Division") -> CorporateAction:
    """A split from a reviewed row citing NSE's circulars, in none of NSE's lists."""
    return CorporateAction(
        symbol="X",
        series="EQ",
        ex_date=ex_date,
        subject=subject,
        kind="split",
        factor=factor,
        nse_list=None,
        citation=CIRCULAR,
    )


def test_a_cited_split_no_nse_list_has_adjusts_like_a_listed_one() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]

    (adjusted,) = adjust(bars, [cited(JUL_3, Fraction(1, 5))], as_of=JUL_8)

    assert (adjusted.close, adjusted.volume, adjusted.factor) == (D("20.00"), 50, Fraction(1, 5))


def test_a_cited_split_within_a_week_of_a_listed_adjusting_action_is_not_applied() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    listed = action(JUL_8, "split", Fraction(1, 5), "Face Value Split From Rs 10 To Rs 2")

    (adjusted,) = adjust(bars, [cited(JUL_3, Fraction(1, 5)), listed], as_of=JUL_8)

    assert adjusted.factor == Fraction(1, 5)


def test_a_cited_split_more_than_a_week_from_a_listed_one_is_applied_beside_it() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    listed = action(date(2024, 7, 11), "bonus", Fraction(1, 2), "Bonus 1:1")

    (adjusted,) = adjust(bars, [cited(JUL_3, Fraction(1, 5)), listed], as_of=date(2024, 7, 31))

    assert adjusted.factor == Fraction(1, 10)


def test_a_listed_dividend_beside_a_cited_split_supersedes_nothing() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    dividend = action(JUL_3, "dividend", None, "Dividend - Rs 8 Per Share")

    (adjusted,) = adjust(bars, [cited(JUL_3, Fraction(1, 5)), dividend], as_of=JUL_8)

    assert (adjusted.factor, adjusted.unadjusted) == (Fraction(1, 5), (dividend,))


def test_a_listed_action_not_yet_known_on_the_as_of_date_supersedes_nothing_then() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    listed = action(JUL_8, "split", Fraction(1, 5), "Face Value Split From Rs 10 To Rs 2")

    (adjusted,) = adjust(bars, [cited(JUL_3, Fraction(1, 5)), listed], as_of=JUL_5)

    assert adjusted.factor == Fraction(1, 5)


def test_supersessions_name_each_cited_date_with_the_listed_actions_and_both_factors() -> None:
    split = cited(JUL_3, Fraction(1, 2), "Sub-Division from Rs. 10/- to Rs. 5/-")
    bonus = CorporateAction(
        symbol="X",
        series="EQ",
        ex_date=JUL_3,
        subject="Bonus 1:1",
        kind="bonus",
        factor=Fraction(1, 2),
        nse_list=None,
        citation=CIRCULAR,
    )
    listed = action(JUL_2, "split", Fraction(1, 2), "Face Value Split From Rs 10 To Rs 5")
    listed_under_be = CorporateAction(
        symbol="X",
        series="BE",
        ex_date=JUL_2,
        subject="Face Value Split From Rs 10 To Rs 5",
        kind="split",
        factor=Fraction(1, 2),
    )
    unrelated = cited(date(2024, 9, 2), Fraction(1, 10))

    (found,) = superseded([split, bonus, listed, listed_under_be, unrelated])

    assert found.cited == (split, bonus)
    assert found.listed == (listed,)
    assert (found.cited_factor, found.listed_factor) == (Fraction(1, 4), Fraction(1, 2))
    assert not found.agrees


def test_a_cited_split_identical_to_a_listed_one_is_superseded_and_agrees() -> None:
    listed = action(JUL_3, "split", Fraction(1, 5), "Face Value Split From Rs 10 To Rs 2")
    row = cited(JUL_3, Fraction(1, 5))

    (found,) = superseded([listed, row])

    assert (found.cited, found.listed, found.agrees) == ((row,), (listed,), True)


def test_every_cited_action_of_a_superseded_ex_date_is_dropped_even_one_reworded() -> None:
    bars = [bar(JUL_1, "100.00", "100.00", "100.00", "100.00", 10)]
    listed = action(JUL_3, "split", Fraction(1, 5), "Face Value Split From Rs 10 To Rs 2")
    reworded = cited(JUL_3, Fraction(1, 5), "Sub-Division from Rs. 10/- to Rs. 2/-")

    (adjusted,) = adjust(bars, [cited(JUL_3, Fraction(1, 5)), reworded, listed], as_of=JUL_8)

    assert adjusted.factor == Fraction(1, 5)


def test_an_action_naming_both_an_nse_list_and_a_citation_or_neither_is_refused() -> None:
    for nse_list, citation in [("equities", CIRCULAR), (None, None)]:
        with pytest.raises(ValueError, match="not both"):
            CorporateAction(
                symbol="X",
                series="EQ",
                ex_date=JUL_3,
                subject="Bonus 1:1",
                kind="bonus",
                factor=Fraction(1, 2),
                nse_list=nse_list,
                citation=citation,
            )
