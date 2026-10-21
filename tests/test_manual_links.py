"""The checked-in manual Instrument Links: read, refused loudly when malformed."""

from datetime import date
from pathlib import Path

import pytest

from tradeline.config import load_config
from tradeline.instruments import ManualLink
from tradeline.manual_links import load_manual_links

REPO = Path(__file__).resolve().parents[1]
HEADER = "old_symbol,new_symbol,effective_date,citation\n"
CIRCULAR = "https://archives.nseindia.com/content/circulars/CML52890.pdf"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "manual_links.csv"
    path.write_text(text)
    return path


def test_each_row_is_a_manual_link_and_comment_lines_are_skipped(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "# Reviewed like code: every row cites an official source.\n"
        + HEADER
        + f"RUCHI,PATANJALI,2022-07-13,{CIRCULAR}\n",
    )

    assert load_manual_links(path) == [
        ManualLink(
            old_symbol="RUCHI",
            new_symbol="PATANJALI",
            effective_date=date(2022, 7, 13),
            citation=CIRCULAR,
        )
    ]


def test_a_row_without_a_citation_is_refused_with_the_row_named(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "RUCHI,PATANJALI,2022-07-13,\n")

    with pytest.raises(ValueError, match=r"manual_links.csv line 2: RUCHI to PATANJALI.*citation"):
        load_manual_links(path)


def test_a_row_with_an_unparseable_date_is_refused_with_the_row_named(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + f"RUCHI,PATANJALI,13/07/2022,{CIRCULAR}\n")

    with pytest.raises(
        ValueError, match=r"manual_links.csv line 2: RUCHI to PATANJALI.*13/07/2022"
    ):
        load_manual_links(path)


@pytest.mark.parametrize(
    "row",
    [
        f"RUCHI,PATANJALI,2022-07-13,{CIRCULAR},extra",
        "RUCHI,PATANJALI,2022-07-13",
        f",PATANJALI,2022-07-13,{CIRCULAR}",
        f"RUCHI,RUCHI,2022-07-13,{CIRCULAR}",
    ],
)
def test_a_malformed_row_is_refused_with_its_line_named(tmp_path: Path, row: str) -> None:
    path = write(tmp_path, HEADER + row + "\n")

    with pytest.raises(ValueError, match=r"manual_links.csv line 2"):
        load_manual_links(path)


def test_a_file_without_the_expected_header_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, f"RUCHI,PATANJALI,2022-07-13,{CIRCULAR}\n")

    with pytest.raises(ValueError, match="header"):
        load_manual_links(path)


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nowhere.csv"):
        load_manual_links(tmp_path / "nowhere.csv")


def test_the_repos_configured_file_joins_ruchi_to_patanjali_citing_nse() -> None:
    config = load_config(REPO / "tradeline.toml")

    links = load_manual_links(REPO / config.instruments.manual_links)

    assert ManualLink("RUCHI", "PATANJALI", date(2022, 7, 13), CIRCULAR) in links
