"""The Fresh Period (ADR 0007 amendment), read off the checked-in Trading Calendar: the
trading days after both the consumed Holdout's last day and the version's freeze, long
enough to carry the Backtest Gate at twelve months. 2025-03-14 is a holiday (Holi)."""

from datetime import date

from synthetic_eod import repo_calendar
from tradeline.holdout import fresh_period

CALENDAR = repo_calendar()
HOLDOUT_LAST = date(2026, 9, 30)


def test_a_version_frozen_before_the_holdouts_end_has_its_fresh_period_start_after_it() -> None:
    period = fresh_period(
        CALENDAR,
        holdout_last=date(2024, 9, 30),
        frozen_on=date(2024, 6, 3),
        data_last=date(2024, 10, 31),
    )

    assert (period.first, period.last) == (date(2024, 10, 1), date(2024, 10, 31))
    assert period.twelve_months_to == date(2025, 9, 30)
    assert period.long_enough is False


def test_a_fresh_period_starts_on_the_first_trading_day_after_the_freeze_day() -> None:
    period = fresh_period(
        CALENDAR,
        holdout_last=date(2024, 9, 30),
        frozen_on=date(2025, 3, 13),
        data_last=date(2025, 3, 31),
    )

    assert period.first == date(2025, 3, 17)


def test_a_fresh_period_is_long_enough_once_its_data_reaches_twelve_months() -> None:
    def ending(data_last: date) -> bool:
        return fresh_period(
            CALENDAR,
            holdout_last=date(2024, 9, 30),
            frozen_on=date(2025, 3, 10),
            data_last=data_last,
        ).long_enough

    assert ending(date(2026, 3, 9)) is False
    assert ending(date(2026, 3, 10)) is True


def test_a_version_frozen_now_has_no_fresh_period_yet_and_cannot_reach_twelve_months() -> None:
    period = fresh_period(
        CALENDAR,
        holdout_last=HOLDOUT_LAST,
        frozen_on=date(2026, 10, 24),
        data_last=date(2026, 10, 23),
    )

    assert (period.first, period.last, period.trading_days) == (date(2026, 10, 26), None, 0)
    assert period.twelve_months_to == date(2027, 10, 25)
    # The Trading Calendar ends 2026-12-31, so no stored data can reach it yet.
    assert period.long_enough is False
