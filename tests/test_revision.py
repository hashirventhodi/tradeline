"""A Strategy's code revision and the docstring lint, on copies of the source tree.

Each test copies ``src/tradeline`` to a temporary directory, edits one file
there, and computes ``pullback_v1``'s revision from the copied ``strategy``
package, so the edit never touches the real code.
"""

import shutil
from pathlib import Path

import pytest

from tradeline.revision import code_revision, docstring_reads

SOURCE = Path(__file__).resolve().parents[1] / "src" / "tradeline"
PULLBACK = "tradeline.strategy.pullback"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the ``tradeline`` package."""
    return Path(shutil.copytree(SOURCE, tmp_path / "tradeline"))


def revision(tree: Path) -> str:
    return code_revision(PULLBACK, package_dir=tree / "strategy")


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


def test_the_revision_of_a_copy_is_the_revision_of_the_package(tree: Path) -> None:
    assert revision(tree) == code_revision(PULLBACK)


@pytest.mark.parametrize(
    ("module", "old", "new"),
    [
        ("pullback.py", "FAST = 20", "FAST = 21"),
        ("indicators.py", "abs(bar.low - previous_close))", "abs(bar.low - bar.low))"),
        ("__init__.py", "    rank: int\n", "    rank: int\n    note: str = ''\n"),
        ("pricing.py", "Fraction(tick)) * tick", "Fraction(tick)) * tick * 1"),
    ],
    ids=["the-strategy", "an-imported-module", "the-interface", "plan-pricing"],
)
def test_a_code_change_in_a_strategy_module_changes_the_revision(
    tree: Path, module: str, old: str, new: str
) -> None:
    before = revision(tree)
    edit(tree / "strategy" / module, old, new)

    assert revision(tree) != before


@pytest.mark.parametrize(
    ("module", "old", "new"),
    [
        ("pullback.py", "buy a pullback to the 20-day average", "buy a dip to the 20-day SMA"),
        ("indicators.py", '"""The simple average of the last', '"""The mean of the last'),
        ("pullback.py", "FAST = 20\n", "FAST = 20  # the fast average\n\n\n"),
        (
            "pullback.py",
            'stop = entry - self.k * candidate.facts["atr14"]',
            "stop = (\n            entry\n"
            "            - self.k * candidate.facts['atr14']\n        )",
        ),
    ],
    ids=["a-module-docstring", "a-function-docstring", "a-comment", "formatting"],
)
def test_a_docstring_comment_or_formatting_change_leaves_the_revision(
    tree: Path, module: str, old: str, new: str
) -> None:
    before = revision(tree)
    edit(tree / "strategy" / module, old, new)

    assert revision(tree) == before


def test_a_change_in_queries_or_adjust_leaves_the_revision(tree: Path) -> None:
    before = revision(tree)
    edit(tree / "queries.py", "if end > as_of:", "if end >= as_of:")
    edit(tree / "adjust.py", "TICK = ", "TICK_UNUSED = 1\nTICK = ")

    assert revision(tree) == before


def test_strategy_code_reads_no_docstring() -> None:
    assert docstring_reads() == []


@pytest.mark.parametrize(
    "line",
    [
        "NOTE = Candidate.__doc__",
        "NOTE = getattr(Candidate, '__doc__')",
        "import inspect\nNOTE = inspect.getdoc(Candidate)",
        "NOTE = __doc__",
    ],
    ids=["attribute", "getattr", "inspect-getdoc", "module-doc"],
)
def test_the_docstring_lint_fails_on_strategy_code_that_reads_a_docstring(
    tree: Path, line: str
) -> None:
    pullback = tree / "strategy" / "pullback.py"
    pullback.write_text(pullback.read_text() + f"\n{line}\n")

    last_line = len(pullback.read_text().splitlines())
    assert docstring_reads(tree / "strategy") == [f"strategy/pullback.py:{last_line}"]
