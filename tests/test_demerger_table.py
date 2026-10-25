"""The checked-in demerger table: read whole, each row citing its sources, or refused."""

from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from tradeline.config import load_config
from tradeline.demergers import Demerger, load_demergers

REPO = Path(__file__).resolve().parents[1]
HEADER = "parent,child,ratio,ex_date,listed_on,parent_basis,child_basis,citation\n"
ROW = "ASTER,ASTERNEW,1:10,2022-08-10,2022-08-22,86.49,13.51,NSE/CMTR/65845\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "demergers.csv"
    path.write_text(text)
    return path


def test_the_seeded_table_holds_itc_hotels_and_jio_financial_each_citing_its_sources() -> None:
    table = load_demergers(REPO / load_config(REPO / "tradeline.toml").corporate_actions.demergers)

    assert [
        (d.parent, d.child, d.ratio, d.ex_date, d.listed_on, d.parent_basis, d.child_basis)
        for d in table.rows
    ] == [
        (
            "RELIANCE",
            "JIOFIN",
            Fraction(1),
            date(2023, 7, 20),
            date(2023, 8, 21),
            Decimal("95.32"),
            Decimal("4.68"),
        ),
        (
            "ITC",
            "ITCHOTELS",
            Fraction(1, 10),
            date(2025, 1, 6),
            date(2025, 1, 29),
            Decimal("86.49"),
            Decimal("13.51"),
        ),
    ]
    # Each cites NSE's circular for the Ex-Date and the company's apportionment of cost.
    for row, circular in zip(table.rows, ("NSE/CMTR/57524", "NSE/CMTR/65845"), strict=True):
        references = [r.strip() for r in row.citation.split(";")]
        assert circular in references
        assert any(r.startswith("https://") for r in references)


def test_a_row_is_read_with_its_ratio_as_child_shares_per_parent_share(tmp_path: Path) -> None:
    table = load_demergers(write(tmp_path, "# reviewed like code\n" + HEADER + ROW))

    assert table.rows == (
        Demerger(
            parent="ASTER",
            child="ASTERNEW",
            ratio=Fraction(1, 10),
            ex_date=date(2022, 8, 10),
            listed_on=date(2022, 8, 22),
            parent_basis=Decimal("86.49"),
            child_basis=Decimal("13.51"),
            citation="NSE/CMTR/65845",
            line=3,
        ),
    )


def test_a_row_announcing_no_split_of_basis_gives_the_child_none(tmp_path: Path) -> None:
    table = load_demergers(
        write(tmp_path, HEADER + "ASTER,ASTERNEW,1:1,2022-08-10,2022-08-22,,,NSE/CMTR/65845\n")
    )

    (row,) = table.rows
    assert (row.parent_basis, row.child_basis) == (Decimal(100), Decimal(0))


def test_the_hash_is_of_the_rows_and_changes_with_any_edit(tmp_path: Path) -> None:
    first = load_demergers(write(tmp_path, HEADER + ROW)).hash
    commented = load_demergers(write(tmp_path, "# a note\n" + HEADER + ROW)).hash
    edited = load_demergers(write(tmp_path, HEADER + ROW.replace("1:10", "1:9"))).hash

    assert first == commented
    assert edited != first


@pytest.mark.parametrize(
    ("row", "refusal"),
    [
        (ROW.replace(",NSE/CMTR/65845", ","), r"line 2: ASTER.*no citation"),
        (ROW.replace("NSE/CMTR/65845", "the company's notice"), r"line 2: ASTER.*not an NSE"),
        (ROW.replace("1:10", "one for ten"), r"line 2: ASTER.*ratio 'one for ten'"),
        (ROW.replace("1:10", "0:10"), r"line 2: ASTER.*ratio '0:10'"),
        (ROW.replace("86.49", "86.4x"), r"line 2: ASTER.*basis"),
        (ROW.replace("86.49", "86.50"), r"line 2: ASTER.*sum to 100"),
        (ROW.replace(",13.51,", ",,"), r"line 2: ASTER.*both or neither"),
        (ROW.replace("2022-08-22", "2022-08-09"), r"line 2: ASTER.*listed before"),
        (ROW.replace("2022-08-10", "10-08-2022"), r"line 2: ASTER.*not a date"),
        (ROW.replace("ASTER,", ","), r"line 2.*parent and the child"),
    ],
)
def test_a_bad_row_refuses_the_whole_table(tmp_path: Path, row: str, refusal: str) -> None:
    with pytest.raises(ValueError, match=refusal):
        load_demergers(write(tmp_path, HEADER + row))
