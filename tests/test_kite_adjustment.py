"""Kite's Adjustment: Kite's closes replayed from NSE's raw closes, stage by stage.

Each test builds one Instrument and series's compared closes, NSE's raw close and
Kite's, from a known adjustment worked out here with ``Decimal`` (the module
under test works in ``Fraction``), and the Corporate Actions behind it,
interpreted from their NSE subjects as ingest does.
"""

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from fractions import Fraction

from tradeline.adjust import CorporateAction, interpret
from tradeline.kite_adjustment import Close, Replay, Stage, replay

PAISA, FIVE_PAISA = Decimal("0.01"), Decimal("0.05")
RUPEE, FIVE_RUPEES = Decimal(1), Decimal(5)


def action(ex_date: str, subject: str) -> CorporateAction:
    kind, factor = interpret(subject)
    return CorporateAction(
        symbol="X", series="EQ", ex_date=date.fromisoformat(ex_date), subject=subject,
        kind=kind, factor=factor,
    )  # fmt: skip


def to_tick(value: Decimal, tick: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    return (value / tick).quantize(Decimal(1), rounding) * tick


def weekdays(first: str, count: int) -> list[date]:
    day, found = date.fromisoformat(first), list[date]()
    while len(found) < count:
        if day.weekday() < 5:
            found.append(day)
        day += timedelta(days=1)
    return found


def prices(count: int, start: str = "2900.15", step: str = "13.35") -> list[Decimal]:
    """NSE closes on the 5 paisa tick, rising and falling so no one line fits them."""
    return [
        Decimal(start) + Decimal(step) * (i % 7) - Decimal("4.40") * (i % 3) for i in range(count)
    ]


def compared(
    days: Sequence[date], nse: Sequence[Decimal], kite: Callable[[date, Decimal], Decimal]
) -> list[Close]:
    return [Close(d, n, kite(d, n)) for d, n in zip(days, nse, strict=True)]


def replayed(closes: Sequence[Close], actions: Sequence[CorporateAction]) -> Replay:
    """The replay, NSE's closes being those of the compared days."""
    return replay(closes, actions, nse_closes={c.trade_date: c.nse for c in closes})


def test_a_later_bonus_rounded_to_five_rupees_is_one_computed_stage_explaining_every_close() -> (
    None
):
    days = weekdays("2024-07-01", 25)
    closes = compared(days, prices(25), lambda _, n: to_tick(n / 2, FIVE_RUPEES))

    result = replayed(closes, [action("2024-10-28", "Bonus 1:1")])

    ((basis),) = result.bases
    assert basis.stages == (
        Stage(
            ex_date=date(2024, 10, 28), form="split", multiplier=Fraction(1, 2),
            subtracted=Fraction(0), tick=Fraction(5), ties="half_even",
        ),
    )  # fmt: skip
    assert (basis.start, basis.end, basis.bars) == (days[0], days[-1], 25)
    assert set(result.explained) == set(days)


def test_a_kite_basis_of_fewer_than_twenty_bars_explains_nothing() -> None:
    days = weekdays("2024-07-01", 19)
    closes = compared(days, prices(19), lambda _, n: to_tick(n / 2, FIVE_RUPEES))

    result = replayed(closes, [action("2024-10-28", "Bonus 1:1")])

    ((basis),) = result.bases
    assert basis.bars == 19
    assert not basis.explains
    assert result.explained == {}


def test_a_close_off_the_replay_in_a_standing_stretch_stays_unexplained_alone() -> None:
    days = weekdays("2024-07-01", 25)
    closes = compared(days, prices(25), lambda _, n: to_tick(n / 2, FIVE_RUPEES))
    # Five rupees off: one close of 25, within the 5 percent (two closes) a stretch may have.
    closes[3] = Close(days[3], closes[3].nse, closes[3].kite + 5)

    result = replayed(closes, [action("2024-10-28", "Bonus 1:1")])

    assert set(result.explained) == set(days) - {days[3]}
    assert result.not_standing == ()


def test_a_stretch_with_more_than_five_percent_of_its_closes_off_does_not_stand() -> None:
    days = weekdays("2024-07-01", 25)
    closes = compared(days, prices(25), lambda _, n: to_tick(n / 2, FIVE_RUPEES))
    for i in (3, 9, 15):
        closes[i] = Close(days[i], closes[i].nse, closes[i].kite + 5)

    result = replayed(closes, [action("2024-10-28", "Bonus 1:1")])

    assert result.bases == ()
    assert result.explained == {}
    assert result.not_standing == ((days[0], days[-1]),)


def split_at(days: Sequence[date], ex_date: str) -> int:
    """How many of ``days`` fall before ``ex_date``."""
    return sum(1 for d in days if d < date.fromisoformat(ex_date))


def test_a_dividend_from_july_2023_to_september_2024_can_be_subtracted() -> None:
    # 25 days before RELIANCE-like Rs 10 dividend of 19 August 2024, 20 from it; a
    # bonus later. Kite subtracted the dividend, rounded, then halved for the bonus.
    days = weekdays("2024-07-15", 45)
    before = split_at(days, "2024-08-19")

    def kite(d: date, n: Decimal) -> Decimal:
        if d < date(2024, 8, 19):
            n = to_tick(n - 10, FIVE_PAISA)
        return to_tick(n / 2, FIVE_PAISA)

    closes = compared(days, prices(45), kite)
    actions = [
        action("2024-08-19", "Dividend - Rs 10 Per Share"),
        action("2024-10-28", "Bonus 1:1"),
    ]

    result = replayed(closes, actions)

    newer, older = result.bases
    assert [(s.ex_date, s.form) for s in newer.stages] == [(date(2024, 10, 28), "split")]
    ((stage),) = older.stages
    assert (stage.ex_date, stage.form, stage.multiplier, stage.subtracted) == (
        date(2024, 8, 19), "dividend_subtracted", Fraction(1), Fraction(10),
    )  # fmt: skip
    assert older.chain == (stage, *newer.stages)
    assert (older.bars, newer.bars) == (before, 45 - before)
    assert set(result.explained) == set(days)


def test_a_dividend_from_october_2024_divides_by_its_ratio_rounded_to_six_decimals() -> None:
    days = weekdays("2024-09-23", 40)
    nse = prices(40, start="1850.05", step="9.10")
    previous = nse[split_at(days, "2024-10-29") - 1]
    # P / (P - D) for INFY's Rs 21 of 29 October 2024, rounded to six decimals.
    divisor = (previous / (previous - 21)).quantize(Decimal("0.000001"))

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / divisor, FIVE_PAISA) if d < date(2024, 10, 29) else n

    result = replayed(
        compared(days, nse, kite), [action("2024-10-29", "Interim Dividend - Rs 21 Per Share")]
    )

    raw, older = result.bases
    assert raw.stages == ()
    ((stage),) = older.stages
    assert (stage.form, stage.divisor) == ("dividend_ratio", Fraction(divisor))
    assert set(result.explained) == {d for d in days if d < date(2024, 10, 29)}


def test_a_dividend_before_july_2023_is_never_a_stage() -> None:
    days = weekdays("2022-06-01", 45)

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n - 10, FIVE_PAISA) if d < date(2022, 7, 1) else n

    result = replayed(
        compared(days, prices(45), kite), [action("2022-07-01", "Dividend - Rs 10 Per Share")]
    )

    ((raw),) = result.bases
    assert raw.stages == ()
    assert result.explained == {}
    assert result.not_standing == ((days[0], days[split_at(days, "2022-07-01") - 1]),)


def test_a_dividend_from_july_2023_can_carry_a_stage_that_only_rounds_again() -> None:
    days = weekdays("2024-07-15", 45)

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n, RUPEE) if d < date(2024, 8, 19) else n

    result = replayed(
        compared(days, prices(45), kite), [action("2024-08-19", "Dividend - Rs 2 Per Share")]
    )

    _, older = result.bases
    ((stage),) = older.stages
    assert (stage.form, stage.multiplier, stage.subtracted, stage.tick) == (
        "reround", Fraction(1), Fraction(0), Fraction(1),
    )  # fmt: skip


def test_a_bonus_of_one_for_three_can_divide_by_its_ratio_rounded_to_two_decimals() -> None:
    days = weekdays("2024-07-01", 25)
    closes = compared(days, prices(25), lambda _, n: to_tick(n / Decimal("1.33"), FIVE_PAISA))

    result = replayed(closes, [action("2024-10-28", "Bonus 1:3")])

    ((basis),) = result.bases
    ((stage),) = basis.stages
    assert (stage.form, stage.divisor) == ("split_2dp", Fraction(133, 100))
    assert set(result.explained) == set(days)


def test_a_stage_rounds_an_exact_half_tick_by_the_tie_rule_its_closes_show() -> None:
    # NSE's closes on odd multiples of 5 paisa halve to exact half ticks, rounded down.
    days = weekdays("2024-07-01", 25)
    closes = compared(
        days, prices(25), lambda _, n: to_tick(n / 2, FIVE_PAISA, rounding=ROUND_HALF_DOWN)
    )

    result = replayed(closes, [action("2024-10-28", "Bonus 1:1")])

    ((basis),) = result.bases
    assert [s.ties for s in basis.stages] == ["half_down"]
    assert set(result.explained) == set(days)


RIGHTS = "Rights 1:10 @ Premium Rs 100/-"


def test_a_rights_issue_is_a_fitted_stage_read_from_kites_closes() -> None:
    days = weekdays("2024-07-01", 45)

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / Decimal("1.0473"), FIVE_PAISA) if d < date(2024, 8, 5) else n

    result = replayed(compared(days, prices(45), kite), [action("2024-08-05", RIGHTS)])

    _, older = result.bases
    ((stage),) = older.stages
    assert stage.fitted and older.fitted
    assert abs(stage.divisor - Fraction("1.0473")) < Fraction(1, 2000)
    assert set(result.explained) == {d for d in days if d < date(2024, 8, 5)}


def test_a_subject_naming_a_dividend_and_a_rights_issue_is_a_fitted_stage() -> None:
    # Stored as a rights issue (HATSUN 2018, SINTEX 2016), as interpret's precedence gives it.
    days = weekdays("2024-10-01", 45)
    subject = "Interim Dividend- Rs 3 Per Share / Rights 1:16 @ Premium Rs 554 Per Share"

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / Decimal("1.0473"), FIVE_PAISA) if d < date(2024, 11, 4) else n

    result = replayed(compared(days, prices(45), kite), [action("2024-11-04", subject)])

    _, older = result.bases
    ((stage),) = older.stages
    assert stage.fitted and older.fitted
    assert abs(stage.divisor - Fraction("1.0473")) < Fraction(1, 2000)
    assert set(result.explained) == {d for d in days if d < date(2024, 11, 4)}


def test_a_fitted_ratio_within_a_tenth_of_a_percent_of_one_is_refused() -> None:
    days = weekdays("2024-07-01", 45)

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n, RUPEE) if d < date(2024, 8, 5) else n

    result = replayed(compared(days, prices(45), kite), [action("2024-08-05", RIGHTS)])

    ((raw),) = result.bases
    assert result.explained == {}
    assert result.not_standing == ((days[0], days[split_at(days, "2024-08-05") - 1]),)


def test_a_fitted_ratio_absorbing_a_dividend_that_is_never_a_stage_is_refused() -> None:
    days = weekdays("2022-06-01", 45)
    previous = prices(45)[split_at(days, "2022-07-01") - 1]

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / (previous / (previous - 150)), FIVE_PAISA) if d < date(2022, 7, 1) else n

    actions = [action("2022-07-01", "Dividend - Rs 150 Per Share"), action("2022-07-01", RIGHTS)]

    result = replayed(compared(days, prices(45), kite), actions)

    ((raw),) = result.bases
    assert result.explained == {}


def test_a_fitted_stage_carries_only_what_a_computed_stage_in_its_gap_leaves() -> None:
    # A bonus on 1 August and a demerger on 5 August, no compared day between them.
    days = [d for d in weekdays("2024-07-01", 47) if not date(2024, 8, 1) <= d < date(2024, 8, 5)]

    def kite(d: date, n: Decimal) -> Decimal:
        if d < date(2024, 8, 1):
            n = to_tick(n * 2 / 3, FIVE_PAISA)
        return to_tick(n / Decimal("1.117"), FIVE_PAISA) if d < date(2024, 8, 5) else n

    actions = [action("2024-08-01", "Bonus 1:2"), action("2024-08-05", "Demerger")]

    result = replayed(compared(days, prices(45), kite), actions)

    _, older = result.bases
    bonus, demerger = older.stages
    assert (bonus.ex_date, bonus.form, bonus.multiplier) == (
        date(2024, 8, 1),
        "split",
        Fraction(2, 3),
    )
    assert (demerger.ex_date, demerger.fitted) == (date(2024, 8, 5), True)
    assert abs(demerger.divisor - Fraction("1.117")) < Fraction(1, 2000)
    assert set(result.explained) == {d for d in days if d < date(2024, 8, 1)}


def test_stretches_join_one_kite_basis_across_an_ex_date_that_carries_no_stage() -> None:
    days = weekdays("2022-06-01", 45)
    closes = compared(days, prices(45), lambda _, n: to_tick(n / 2, FIVE_RUPEES))
    actions = [
        action("2022-07-01", "Dividend - Rs 10 Per Share"),
        action("2024-10-28", "Bonus 1:1"),
    ]

    result = replayed(closes, actions)

    ((basis),) = result.bases
    assert [s.form for s in basis.stages] == ["split"]
    assert (basis.start, basis.end, basis.bars) == (days[0], days[-1], 45)
    assert set(result.explained) == set(days)


def test_a_dividends_p_is_nses_close_the_day_before_even_where_kite_has_no_candle() -> None:
    days = weekdays("2024-09-23", 40)
    nse = prices(40, start="1850.05", step="9.10")
    before = split_at(days, "2024-10-29")
    # P is 28 October's NSE close, a day Kite has no candle for.
    divisor = (nse[before - 1] / (nse[before - 1] - 21)).quantize(Decimal("0.000001"))

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / divisor, FIVE_PAISA) if d < date(2024, 10, 29) else n

    closes = compared(days, nse, kite)
    del closes[before - 1]

    result = replay(
        closes,
        [action("2024-10-29", "Interim Dividend - Rs 21 Per Share")],
        nse_closes=dict(zip(days, nse, strict=True)),
    )

    _, older = result.bases
    ((stage),) = older.stages
    assert (stage.form, stage.divisor) == ("dividend_ratio", Fraction(divisor))


def test_a_fitted_stage_that_could_sit_at_either_of_two_actions_is_refused_and_reported() -> None:
    # A rights issue on 1 August and a buyback on 5 August, no compared day between them.
    days = [d for d in weekdays("2024-07-01", 47) if not date(2024, 8, 1) <= d < date(2024, 8, 5)]

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / Decimal("1.0473"), FIVE_PAISA) if d < date(2024, 8, 1) else n

    actions = [action("2024-08-01", RIGHTS), action("2024-08-05", "Buy Back")]

    result = replayed(compared(days, prices(45), kite), actions)

    ((raw),) = result.bases
    assert result.explained == {}
    older = (days[0], days[split_at(days, "2024-08-01") - 1])
    assert result.not_standing == (older,)
    ((ambiguous),) = result.ambiguous_fits
    assert (ambiguous.stretch, ambiguous.ex_dates) == (
        older, (date(2024, 8, 1), date(2024, 8, 5)),
    )  # fmt: skip


def test_a_gap_of_more_layouts_than_are_tried_one_by_one_is_reported_as_a_bounded_search() -> None:
    # Four dividends and a rights issue between two stretches: each dividend has no
    # stage or one of three, 4 ** 4 = 256 layouts. Kite subtracted the Rs 30 alone.
    gap = ["2024-08-01", "2024-08-02", "2024-08-05", "2024-08-06"]
    days = [d for d in weekdays("2024-07-01", 49) if not date(2024, 8, 1) <= d < date(2024, 8, 8)]

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n - 30, FIVE_PAISA) if d < date(2024, 8, 5) else n

    actions = [
        action(gap[0], "Dividend - Rs 1 Per Share"),
        action(gap[1], "Dividend - Rs 1.50 Per Share"),
        action(gap[2], "Dividend - Rs 30 Per Share"),
        action(gap[3], "Dividend - Rs 2 Per Share"),
        action("2024-08-07", RIGHTS),
    ]

    result = replayed(compared(days, prices(len(days)), kite), actions)

    ((bounded),) = result.bounded_searches
    older = (days[0], days[split_at(days, "2024-08-01") - 1])
    assert (bounded.stretch, bounded.layouts, bounded.stands) == (older, 256, True)
    assert set(result.explained) == {d for d in days if d < date(2024, 8, 1)}


def test_a_group_whose_computed_stages_need_different_ticks_is_not_reproduced() -> None:
    # ADR 0018: the stages found for one gap share one tick. The bonus of 1 August
    # rounds to the rupee and the dividend of 2 August to the paisa, and no compared
    # day falls between them and the rights issue of 5 August, so all three are one gap.
    days = [d for d in weekdays("2024-07-01", 48) if not date(2024, 8, 1) <= d < date(2024, 8, 6)]

    def kite(d: date, n: Decimal) -> Decimal:
        if d < date(2024, 8, 1):
            return to_tick(n / 2, RUPEE) - Decimal("10.37")
        return n

    actions = [
        action("2024-08-01", "Bonus 1:1"),
        action("2024-08-02", "Dividend - Rs 10.37 Per Share"),
        action("2024-08-05", RIGHTS),
    ]

    result = replayed(compared(days, prices(len(days)), kite), actions)

    assert result.explained == {}
    assert result.not_standing == ((days[0], days[split_at(days, "2024-08-01") - 1]),)


def test_a_fitted_ratio_repeating_a_staged_split_of_its_gap_is_refused() -> None:
    # Kite's closes halved twice: the bonus of 1 August staged, and a rights issue on
    # 5 August whose fitted ratio would be the bonus's 2 again, which absorbs it.
    days = [d for d in weekdays("2024-07-01", 47) if not date(2024, 8, 1) <= d < date(2024, 8, 5)]

    def kite(d: date, n: Decimal) -> Decimal:
        if d < date(2024, 8, 1):
            return to_tick(to_tick(n / 2, FIVE_PAISA) / 2, FIVE_PAISA)
        return n

    actions = [action("2024-08-01", "Bonus 1:1"), action("2024-08-05", RIGHTS)]

    result = replayed(compared(days, prices(len(days)), kite), actions)

    assert result.explained == {}
    assert result.not_standing == ((days[0], days[split_at(days, "2024-08-01") - 1]),)


def test_a_bounded_search_whose_stretch_does_not_stand_is_reported_as_such() -> None:
    # The same gap of 256 layouts, but Kite's older closes are Rs 30 off on odd days and
    # Rs 31 off on even ones: no layout reproduces them, and the bound may have hidden one.
    gap = ["2024-08-01", "2024-08-02", "2024-08-05", "2024-08-06"]
    days = [d for d in weekdays("2024-07-01", 49) if not date(2024, 8, 1) <= d < date(2024, 8, 8)]

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n - 30 - d.day % 2, FIVE_PAISA) if d < date(2024, 8, 5) else n

    actions = [
        action(gap[0], "Dividend - Rs 1 Per Share"),
        action(gap[1], "Dividend - Rs 1.50 Per Share"),
        action(gap[2], "Dividend - Rs 30 Per Share"),
        action(gap[3], "Dividend - Rs 2 Per Share"),
        action("2024-08-07", RIGHTS),
    ]

    result = replayed(compared(days, prices(len(days)), kite), actions)

    ((bounded),) = result.bounded_searches
    assert (bounded.layouts, bounded.stands) == (256, False)
    assert result.explained == {}
    assert result.not_standing == ((days[0], days[split_at(days, "2024-08-01") - 1]),)


def test_stages_of_different_kite_bases_round_ties_by_different_rules() -> None:
    # A bonus on 19 August rounding to 5 paisa, ties up; then one on 28 October rounding
    # to the paisa, ties down. Halving a 5 paisa price lands on 2.5 paisa, so each
    # stage meets exact half ticks, and the paisa keeps the earlier stage's rounding.
    days = weekdays("2024-07-15", 45)

    def kite(d: date, n: Decimal) -> Decimal:
        if d < date(2024, 8, 19):
            n = to_tick(n / 2, FIVE_PAISA, rounding=ROUND_HALF_UP)
        return to_tick(n / 2, PAISA, rounding=ROUND_HALF_DOWN)

    actions = [action("2024-08-19", "Bonus 1:1"), action("2024-10-28", "Bonus 1:1")]

    result = replayed(compared(days, prices(45), kite), actions)

    newer, older = result.bases
    assert [(s.ex_date, s.ties) for s in newer.stages] == [(date(2024, 10, 28), "half_down")]
    assert [(s.ex_date, s.ties) for s in older.stages] == [(date(2024, 8, 19), "half_up")]
    assert set(result.explained) == set(days)


def test_a_subject_that_misspells_dividend_as_divided_is_a_dividend() -> None:
    # NSE's own spelling (ABBOTINDIA 2016, NELCAST 2015), stored as "other" before #15.
    days = weekdays("2024-09-23", 40)
    nse = prices(40, start="1850.05", step="9.10")
    previous = nse[split_at(days, "2024-10-29") - 1]
    divisor = (previous / (previous - 35)).quantize(Decimal("0.000001"))

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n / divisor, FIVE_PAISA) if d < date(2024, 10, 29) else n

    subject = "Annual General Meeting/Divided - Rs 35/- Per Share"

    result = replayed(compared(days, nse, kite), [action("2024-10-29", subject)])

    _, older = result.bases
    ((stage),) = older.stages
    assert (stage.form, stage.divisor) == ("dividend_ratio", Fraction(divisor))


def test_a_bounded_search_for_an_ambiguous_fit_is_reported_with_the_ex_date_it_tried() -> None:
    # Four dividends and two rights issues between two stretches, Kite's older closes
    # unreproducible: the gap's own search is bounded, and so is the search at each
    # rights issue for a fitted stage that would make the fit ambiguous.
    gap = ["2024-08-01", "2024-08-02", "2024-08-05", "2024-08-06"]
    days = [d for d in weekdays("2024-07-01", 50) if not date(2024, 8, 1) <= d < date(2024, 8, 9)]

    def kite(d: date, n: Decimal) -> Decimal:
        return to_tick(n - 30 - d.day % 2, FIVE_PAISA) if d < date(2024, 8, 5) else n

    actions = [
        action(gap[0], "Dividend - Rs 1 Per Share"),
        action(gap[1], "Dividend - Rs 1.50 Per Share"),
        action(gap[2], "Dividend - Rs 30 Per Share"),
        action(gap[3], "Dividend - Rs 2 Per Share"),
        action("2024-08-07", RIGHTS),
        action("2024-08-08", RIGHTS),
    ]

    result = replayed(compared(days, prices(len(days)), kite), actions)

    assert [(b.fitted_at, b.layouts, b.stands) for b in result.bounded_searches] == [
        (None, 256, False),
        (date(2024, 8, 7), 256, False),
        (date(2024, 8, 8), 256, False),
    ]
    assert result.ambiguous_fits == ()


def test_each_dividend_of_a_gap_without_a_fittable_action_can_have_its_own_stage() -> None:
    # Rs 10 subtracted on 1 August and Rs 5 on 5 August, no compared day between them:
    # neither amount alone reproduces the Rs 15 Kite took off, so both are stages.
    days = [d for d in weekdays("2024-07-01", 47) if not date(2024, 8, 1) <= d < date(2024, 8, 5)]

    def kite(d: date, n: Decimal) -> Decimal:
        if d < date(2024, 8, 1):
            n = to_tick(n - 10, FIVE_PAISA)
        return to_tick(n - 5, FIVE_PAISA) if d < date(2024, 8, 5) else n

    actions = [
        action("2024-08-01", "Dividend - Rs 10 Per Share"),
        action("2024-08-05", "Dividend - Rs 5 Per Share"),
    ]

    result = replayed(compared(days, prices(len(days)), kite), actions)

    _, older = result.bases
    assert [(s.ex_date, s.form, s.subtracted) for s in older.stages] == [
        (date(2024, 8, 1), "dividend_subtracted", Fraction(10)),
        (date(2024, 8, 5), "dividend_subtracted", Fraction(5)),
    ]
    assert set(result.explained) == {d for d in days if d < date(2024, 8, 1)}
