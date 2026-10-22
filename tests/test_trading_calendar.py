"""The checked-in Trading Calendar: read, refused loudly when malformed."""

from datetime import date
from pathlib import Path

import pytest

from tradeline.trading_calendar import CalendarDay, TradingCalendar, load_trading_calendar

COVERS = "covers,2024-01-01,2024-12-31\n"
HEADER = "date,kind,citation\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "trading_calendar.csv"
    path.write_text(text)
    return path


def test_the_coverage_and_each_row_are_read_and_comments_are_skipped(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "# Reviewed like code: every row cites NSE's circular.\n"
        + COVERS
        + HEADER
        + "2024-08-15,holiday,NSE/CMTR/59722\n"
        + "\n"
        + '2024-11-01,special_session,"https://nsearchives.nseindia.com/content/circulars/CMTR64476.pdf"\n',
    )

    calendar = load_trading_calendar(path)

    assert calendar == TradingCalendar(
        date(2024, 1, 1),
        date(2024, 12, 31),
        (
            CalendarDay(date(2024, 8, 15), "holiday", "NSE/CMTR/59722", 4),
            CalendarDay(
                date(2024, 11, 1),
                "special_session",
                "https://nsearchives.nseindia.com/content/circulars/CMTR64476.pdf",
                6,
            ),
        ),
    )
    assert calendar.special_sessions == {date(2024, 11, 1)}
    assert calendar.holidays == {date(2024, 8, 15)}


def test_trading_days_are_weekdays_but_holidays_and_every_special_session(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path,
        COVERS
        + HEADER
        + "2024-08-15,holiday,NSE/CMTR/59722\n"
        + "2024-08-17,special_session,NSE/CMTR/1\n",
    )

    days = list(load_trading_calendar(path).trading_days(date(2024, 8, 14), date(2024, 8, 19)))

    assert days == [date(2024, 8, 14), date(2024, 8, 16), date(2024, 8, 17), date(2024, 8, 19)]


@pytest.mark.parametrize(
    ("row", "complaint"),
    [
        ("2024-08-15,holiday\n", "expected 3 columns"),
        ("2024-08-15,holiday,NSE/CMTR/1,extra\n", "expected 3 columns"),
        ("15-08-2024,holiday,NSE/CMTR/1\n", "date '15-08-2024' is not a date"),
        ("2024-08-15,holiday,\n", "2024-08-15: no citation"),
        ("2024-08-15,closed,NSE/CMTR/1\n", "kind 'closed' is not one of holiday, special_session"),
        ("2024-08-17,holiday,NSE/CMTR/1\n", "2024-08-17: a holiday on a Saturday"),
        ("2025-01-01,holiday,NSE/CMTR/1\n", "outside the calendar's coverage"),
    ],
)
def test_a_bad_row_refuses_the_file_naming_its_line(
    tmp_path: Path, row: str, complaint: str
) -> None:
    path = write(tmp_path, COVERS + HEADER + "2024-07-17,holiday,NSE/CMTR/1\n" + row)

    with pytest.raises(ValueError, match=rf"trading_calendar.csv line 4: .*{complaint}"):
        load_trading_calendar(path)


def test_a_day_listed_twice_refuses_the_file_naming_both_lines(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        COVERS + HEADER + "2024-08-15,holiday,NSE/CMTR/1\n2024-08-15,special_session,NSE/CMTR/2\n",
    )

    with pytest.raises(ValueError, match="line 4: 2024-08-15 is already listed on line 3"):
        load_trading_calendar(path)


@pytest.mark.parametrize(
    "text",
    [
        HEADER + "2024-08-15,holiday,NSE/CMTR/1\n",
        "covers,2024-12-31,2024-01-01\n" + HEADER,
        "covers,2024-01-01\n" + HEADER,
        "",
    ],
)
def test_a_file_that_does_not_state_its_coverage_first_is_refused(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(ValueError, match="first"):
        load_trading_calendar(write(tmp_path, text))


def test_a_file_without_its_header_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="header date,kind,citation"):
        load_trading_calendar(write(tmp_path, COVERS + "2024-08-15,holiday,NSE/CMTR/1\n"))


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        load_trading_calendar(tmp_path / "trading_calendar.csv")
