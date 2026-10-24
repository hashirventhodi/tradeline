"""Git checkouts for the tests of what a Protocol is frozen at and run under (ADR 0007).

The suite runs from a working tree that is often dirty while code is being
written, so ``conftest.py`` stands every test in at ``TEST_COMMIT`` instead of
the repo's real commit. The tests of the refusal itself make a real checkout in
a temporary directory, commit into it, and point the code at it.
"""

import subprocess
from pathlib import Path

# The commit every test runs at unless it makes a checkout of its own.
TEST_COMMIT = "7e57" * 10


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Tradeline Tests",
            "-c",
            "user.email=tests@example.com",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def committed_checkout(directory: Path) -> Path:
    """A git checkout at ``directory`` with one tracked file, ``code.py``, committed."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "--quiet")
    (directory / "code.py").write_text("STOP = 2\n")
    git(directory, "add", "code.py")
    git(directory, "commit", "--quiet", "-m", "Stop at 2 ATRs")
    return directory


def dirty_checkout(directory: Path) -> Path:
    """A committed checkout whose tracked file has since been changed."""
    checkout = committed_checkout(directory)
    (checkout / "code.py").write_text("STOP = 3\n")
    return checkout
