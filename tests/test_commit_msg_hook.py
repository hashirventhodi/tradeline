"""The ``commit-msg`` hook in ``.githooks``: a subject, a blank line, then the body."""

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "commit-msg"


def run_hook(tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message)
    return subprocess.run([HOOK, path], capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    "message",
    [
        "Count Listing Age from the earliest NSE evidence (#32)\n",
        "Correct the SME counts (#33)\n\nThe body, wrapped.\n\nCo-Authored-By: X <x@example.com>\n",
        # As git's editor leaves it: comment lines, which git strips, are not counted.
        "# Please enter the commit message\nCite the ISIN changes (#33)\n\nBody.\n# a comment\n",
    ],
)
def test_a_subject_then_a_blank_line_then_a_body_is_accepted(tmp_path: Path, message: str) -> None:
    assert run_hook(tmp_path, message).returncode == 0


def test_a_body_run_into_the_subject_is_refused(tmp_path: Path) -> None:
    """As e19053a went out: the body on the line after the subject, with no blank line."""
    result = run_hook(
        tmp_path,
        "Cite the fund, SME, TIDEWATER and KARDA ISIN changes (#33)\n"
        "All 94 changes now have an NSE circular.\n",
    )

    assert result.returncode == 1
    assert "second line must be blank" in result.stderr


def test_a_subject_over_120_characters_is_refused(tmp_path: Path) -> None:
    result = run_hook(tmp_path, "A" * 121 + "\n")

    assert result.returncode == 1
    assert "121 characters" in result.stderr


def test_an_empty_subject_is_refused(tmp_path: Path) -> None:
    assert run_hook(tmp_path, "\n\nA body with no subject.\n").returncode == 1
