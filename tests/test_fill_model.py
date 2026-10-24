"""The Paper Broker's fill model against one day's bar, with no database (ADR 0003 amendment).

Every bar below is built by hand. A Position here was entered at 100.00 with
its stop at 94.00 and its target at 112.00, each on the 5 paisa tick; an exit's
protective band is 5 percent.
"""

from decimal import Decimal

import pytest

from tradeline.paper_broker import (
    Bar,
    Filled,
    Unfilled,
    exit_limit,
    exit_trigger,
    fill_entry,
    fill_exit,
    fill_target,
)

D = Decimal
STOP = D("94.00")
TARGET = D("112.00")


def bar(open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(D(open_), D(high), D(low), D(close))


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (bar("99.50", "103", "98", "101"), Filled(D("99.50"))),
        (bar("100.00", "103", "98", "101"), Filled(D("100.00"))),
        (bar("100.05", "103", "95", "101"), Unfilled("open_above_limit")),
        (bar("99", "99", "99", "99"), Unfilled("circuit_lock")),
    ],
    ids=["open-below", "open-at-limit", "open-above-dips-later", "circuit-lock-below"],
)
def test_an_entry_fills_at_the_open_only_when_the_open_is_at_or_below_its_limit(
    day: Bar, expected: Filled | Unfilled
) -> None:
    assert fill_entry(D("100.00"), day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (bar("108", "112.05", "107", "111"), Filled(TARGET)),
        (bar("108", "112.00", "107", "111"), Unfilled("not_reached")),
        (bar("115", "118", "113", "117"), Filled(D("115"))),
        (bar("113", "113", "113", "113"), Unfilled("circuit_lock")),
        (bar("108", "113", "93", "100"), Unfilled("stop_reached")),
        (bar("114", "116", "94", "95"), Unfilled("stop_reached")),
    ],
    ids=[
        "high-above",
        "high-equal",
        "open-gaps-above",
        "circuit-lock-above",
        "crosses-both",
        "gaps-above-then-falls-to-the-stop",
    ],
)
def test_a_target_fills_at_the_target_when_the_high_is_strictly_above_and_at_a_gapped_open(
    day: Bar, expected: Filled | Unfilled
) -> None:
    assert fill_target(TARGET, day, stop=STOP) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (bar("95.00", "96", "93", "94"), Filled(D("95.00"))),
        (bar("94.05", "96", "93", "94"), Filled(D("94.05"))),
        (bar("94.00", "96", "93", "94"), Unfilled("open_below_limit")),
        (bar("96", "96", "96", "96"), Unfilled("circuit_lock")),
    ],
    ids=["open-above", "open-at-limit", "open-below-the-band", "circuit-lock-above"],
)
def test_a_stop_or_time_exit_fills_at_the_open_only_when_the_open_clears_its_band(
    day: Bar, expected: Filled | Unfilled
) -> None:
    assert fill_exit(D("94.05"), day) == expected


def test_an_exits_limit_is_the_close_less_the_band_rounded_down_to_the_tick() -> None:
    # 99.00 x 0.95 = 94.05, on the tick; 99.10 x 0.95 = 94.145, down to 94.10.
    assert exit_limit(D("99.00"), band=D("0.05"), tick=D("0.05")) == D("94.05")
    assert exit_limit(D("99.10"), band=D("0.05"), tick=D("0.05")) == D("94.10")
    assert exit_limit(D("99.10"), band=D("0.05"), tick=D("0.01")) == D("94.14")


@pytest.mark.parametrize(
    ("day", "days_held", "expected"),
    [
        (bar("100", "103", "94.05", "101"), 3, None),
        (bar("100", "103", "94.00", "101"), 3, "stop"),
        (bar("100", "113", "93", "101"), 3, "stop"),
        (bar("100", "103", "95", "101"), 10, "time"),
        (bar("100", "103", "93", "101"), 10, "stop"),
        (bar("100", "103", "95", "101"), 9, None),
        (bar("90", "90", "90", "90"), 3, "stop"),
    ],
    ids=[
        "low-above-stop",
        "low-at-stop",
        "crosses-both-is-a-stop",
        "time",
        "stop-and-time-is-a-stop",
        "one-day-short",
        "circuit-lock-below",
    ],
)
def test_an_exit_triggers_on_the_completed_bar_a_stop_before_a_time_stop(
    day: Bar, days_held: int, expected: str | None
) -> None:
    assert exit_trigger(day, stop=STOP, days_held=days_held, time_stop_days=10) == expected


def test_a_day_the_instrument_did_not_trade_fills_nothing() -> None:
    assert fill_entry(D("100.00"), None) == Unfilled("no_bar")
    assert fill_exit(D("94.05"), None) == Unfilled("no_bar")
    assert fill_target(TARGET, None, stop=STOP) == Unfilled("no_bar")
