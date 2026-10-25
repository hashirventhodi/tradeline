"""The Benchmark's scheme changes only by a recorded fund-change event (ADR 0015 amendment).

The checked-in ``benchmark_funds.csv`` names the scheme from its first date; a
later row changes it only for the fund's merger, closure or change of index,
from that row's date.
"""

from datetime import date
from pathlib import Path

import pytest

from synthetic_eod import REPO_ROOT
from tradeline.benchmark import load_benchmark_funds

HEADER = "in_force_from,scheme_code,event,citation"
FIRST = "2019-09-11,147625,chosen,ADR 0015 amendment"


def funds_file(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "benchmark_funds.csv"
    path.write_text("\n".join([HEADER, *rows]) + "\n")
    return path


def test_the_checked_in_benchmark_is_motilal_oswal_nifty_500_direct_growth_from_its_first_nav() -> (
    None
):
    funds = load_benchmark_funds(REPO_ROOT / "benchmark_funds.csv")

    assert funds.first == date(2019, 9, 11)
    assert funds.scheme_on(date(2019, 9, 11)) == 147625
    assert funds.scheme_on(date(2026, 10, 23)) == 147625


@pytest.mark.parametrize("event", ["merger", "closure", "index_change"])
def test_a_recorded_fund_change_event_changes_the_scheme_from_its_date(
    tmp_path: Path, event: str
) -> None:
    funds = load_benchmark_funds(
        funds_file(tmp_path, FIRST, f"2025-04-01,152731,{event},AMC notice of 2025-03-01")
    )

    assert funds.scheme_on(date(2025, 3, 31)) == 147625
    assert funds.scheme_on(date(2025, 4, 1)) == 152731
    assert funds.schemes == (147625, 152731)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((FIRST, "2025-04-01,152731,cheaper,X"), "is not a fund-change event"),
        ((FIRST, "2025-04-01,152731,chosen,X"), "is not a fund-change event"),
        (("2019-09-11,147625,merger,X",), "the first row's event must be 'chosen'"),
        ((FIRST, "2025-04-01,152731,merger,"), "no citation"),
        ((FIRST, "2019-09-11,152731,merger,X"), "is not after"),
        ((FIRST, "2025-04-01,147625,merger,X"), "already the Benchmark"),
        ((FIRST, "2025-04-01,abc,merger,X"), "is not an AMFI scheme code"),
        ((), "no rows"),
    ],
)
def test_a_change_without_a_recorded_event_refuses_the_file(
    tmp_path: Path, rows: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_benchmark_funds(funds_file(tmp_path, *rows))


def test_a_day_before_the_first_row_has_no_benchmark() -> None:
    funds = load_benchmark_funds(REPO_ROOT / "benchmark_funds.csv")

    with pytest.raises(ValueError, match="before the Benchmark's first date 2019-09-11"):
        funds.scheme_on(date(2019, 9, 10))
