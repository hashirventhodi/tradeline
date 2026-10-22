"""The checked-in Large Move records: read, refused loudly when malformed."""

from datetime import date
from pathlib import Path

import pytest

from tradeline.config import Config, ConfigError, load_config
from tradeline.large_move_records import LargeMoveRecord, load_large_move_records

HEADER = "symbol,trade_date,reason,citation\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "large_moves.csv"
    path.write_text(text)
    return path


def test_each_row_is_read_and_comments_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "# Reviewed like code: every row cites its source.\n"
        + HEADER
        + "OCCL,2024-07-01,demerger,NSE/CML/00000\n"
        + "\n"
        + 'XYZ,2020-03-23,market,"reviewer: no filing found, a market-wide fall"\n',
    )

    assert load_large_move_records(path) == [
        LargeMoveRecord("OCCL", date(2024, 7, 1), "demerger", "NSE/CML/00000", 3),
        LargeMoveRecord(
            "XYZ", date(2020, 3, 23), "market", "reviewer: no filing found, a market-wide fall", 5
        ),
    ]


def test_an_unknown_reason_refuses_the_file_naming_the_line(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "OCCL,2024-07-01,demerger,x\nXYZ,2020-03-23,crash,x\n")

    with pytest.raises(ValueError, match=r"large_moves.csv line 3: XYZ: reason 'crash'"):
        load_large_move_records(path)


def test_a_row_without_a_citation_refuses_the_file_naming_the_line(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "XYZ,2020-03-23,market,\n")

    with pytest.raises(ValueError, match=r"large_moves.csv line 2: XYZ: no citation"):
        load_large_move_records(path)


@pytest.mark.parametrize(
    ("row", "complaint"),
    [
        ("XYZ,23/03/2020,market,x", "is not a date"),
        ("XYZ,2020-03-23,market", "expected 4 columns"),
        (",2020-03-23,market,x", "the Symbol is required"),
    ],
)
def test_a_malformed_row_refuses_the_file_naming_the_line(
    tmp_path: Path, row: str, complaint: str
) -> None:
    path = write(tmp_path, HEADER + row + "\n")

    with pytest.raises(ValueError, match=rf"large_moves.csv line 2.*{complaint}"):
        load_large_move_records(path)


def test_a_second_row_for_one_symbol_and_day_refuses_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "XYZ,2020-03-23,market,x\nXYZ,2020-03-23,rights,y\n")

    with pytest.raises(ValueError, match=r"line 3: XYZ: 2020-03-23 is already recorded on line 2"):
        load_large_move_records(path)


def test_a_missing_file_or_header_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        load_large_move_records(tmp_path / "absent.csv")
    with pytest.raises(ValueError, match="the first row must be the header"):
        load_large_move_records(write(tmp_path, "XYZ,2020-03-23,market,x\n"))


def test_the_integrity_section_names_the_records_file_and_the_ratio(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text('[integrity]\nlarge_moves = "data/moves.csv"\nlarge_move_ratio = 2.0\n')

    assert load_config(path).integrity == Config.Integrity(
        large_moves="data/moves.csv", large_move_ratio=2.0
    )
    assert Config.Integrity().large_move_ratio == 1.5


def test_a_ratio_of_one_or_less_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[integrity]\nlarge_move_ratio = 1.0\n")

    with pytest.raises(ConfigError, match="large_move_ratio must be more than 1"):
        load_config(path)
