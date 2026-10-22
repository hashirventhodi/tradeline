"""The checked-in cited Corporate Actions: read, refused loudly when malformed."""

from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from tradeline.cited_actions import CitedAction, load_cited_actions
from tradeline.config import load_config

REPO = Path(__file__).resolve().parents[1]
HEADER = "symbol,ex_date,subject,citation\n"
SPLIT = "Sub-Division from Rs. 10/- to Rs. 2/-"
CIRCULARS = "NSE/CML/44773; https://nsearchives.nseindia.com/content/circulars/CML44835.pdf"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cited_corporate_actions.csv"
    path.write_text(text)
    return path


def test_each_row_is_read_with_the_factor_its_subject_gives_and_comments_are_skipped(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path,
        "# Reviewed like code: every row cites NSE's circulars.\n"
        + HEADER
        + f'KARDA,2020-07-02,{SPLIT},"{CIRCULARS}"\n'
        + "\n"
        + 'GICL,2025-10-15,Bonus 1:1,"NSE/CML/70874"\n',
    )

    assert load_cited_actions(path) == [
        CitedAction("KARDA", date(2020, 7, 2), SPLIT, CIRCULARS, "split", Fraction(1, 5), 3),
        CitedAction(
            "GICL", date(2025, 10, 15), "Bonus 1:1", "NSE/CML/70874", "bonus", Fraction(1, 2), 5
        ),
    ]


def test_a_row_without_a_citation_is_refused_with_the_line_named(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + f"KARDA,2020-07-02,{SPLIT},\n")

    with pytest.raises(ValueError, match=r"cited_corporate_actions.csv line 2: KARDA.*citation"):
        load_cited_actions(path)


@pytest.mark.parametrize(
    "citation",
    [
        "https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=1",
        "NSE/CML/44773; KARDA_board_outcome.pdf",
        "the company's record-date notice",
    ],
)
def test_a_row_citing_anything_but_nse_circulars_is_refused(tmp_path: Path, citation: str) -> None:
    path = write(tmp_path, HEADER + f'KARDA,2020-07-02,{SPLIT},"{citation}"\n')

    with pytest.raises(ValueError, match=r"line 2: KARDA.*NSE circular"):
        load_cited_actions(path)


@pytest.mark.parametrize(
    "subject", ["Dividend - Rs 2 Per Share", "Sub-Division", "Bonus Ncrps 1:1"]
)
def test_a_row_whose_subject_gives_no_factor_is_refused(tmp_path: Path, subject: str) -> None:
    path = write(tmp_path, HEADER + f'KARDA,2020-07-02,{subject},"{CIRCULARS}"\n')

    with pytest.raises(ValueError, match=r"line 2: KARDA.*no split or bonus factor"):
        load_cited_actions(path)


def test_a_row_with_a_malformed_date_is_refused_with_the_line_named(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + f'KARDA,02/07/2020,{SPLIT},"{CIRCULARS}"\n')

    with pytest.raises(ValueError, match=r"line 2: KARDA.*02/07/2020"):
        load_cited_actions(path)


def test_one_bad_row_refuses_the_whole_file(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER + f'KARDA,2020-07-02,{SPLIT},"{CIRCULARS}"\n' + f"JSLL,2025-06-12,{SPLIT},\n",
    )

    with pytest.raises(ValueError, match=r"line 3: JSLL"):
        load_cited_actions(path)


@pytest.mark.parametrize(
    "row",
    [
        f'KARDA,2020-07-02,{SPLIT},"{CIRCULARS}",extra',
        "KARDA,2020-07-02",
        f',2020-07-02,{SPLIT},"{CIRCULARS}"',
    ],
)
def test_a_malformed_row_is_refused_with_its_line_named(tmp_path: Path, row: str) -> None:
    path = write(tmp_path, HEADER + row + "\n")

    with pytest.raises(ValueError, match=r"cited_corporate_actions.csv line 2"):
        load_cited_actions(path)


def test_a_file_without_the_expected_header_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, f'KARDA,2020-07-02,{SPLIT},"{CIRCULARS}"\n')

    with pytest.raises(ValueError, match="header"):
        load_cited_actions(path)


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nowhere.csv"):
        load_cited_actions(tmp_path / "nowhere.csv")


def test_the_repos_configured_file_is_read_whole() -> None:
    config = load_config(REPO / "tradeline.toml")

    rows = load_cited_actions(REPO / config.corporate_actions.cited)

    assert rows
    assert all(row.citation for row in rows)
