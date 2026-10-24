"""A Strategy's code revision, and the lint check that keeps it honest (ADR 0007).

The code revision is a SHA-256 over the parsed code of the ``tradeline.strategy``
modules a Strategy's module imports, directly or through one another, with the
package's own ``__init__`` always included. Each module is parsed and dumped
with location information left out and docstrings removed, so a comment, a
docstring, or a formatting change does not change the revision, while any
change to code does. Imports outside the package (``tradeline.queries``,
``tradeline.adjust``) are not followed: a change there is not a new Strategy
version.

Removing docstrings is sound only while no Strategy reads one, since a
docstring that code reads is behaviour. ``docstring_reads`` finds every read
of ``__doc__`` (and ``inspect.getdoc``) in the package; the test suite asserts
there are none.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import tradeline.strategy

PACKAGE = "tradeline.strategy"
PACKAGE_DIR = Path(tradeline.strategy.__file__).parent


def code_revision(module: str, *, package_dir: Path = PACKAGE_DIR) -> str:
    """The code revision of the Strategy defined in ``module`` (a ``tradeline.strategy``
    module), read from the package's source files under ``package_dir``."""
    pending, seen = [PACKAGE, module], set[str]()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(_imports(_parse(name, package_dir), name, package_dir))
    digest = hashlib.sha256()
    for name in sorted(seen):
        tree = _without_docstrings(_parse(name, package_dir))
        digest.update(name.encode() + b"\0")
        digest.update(ast.dump(tree, include_attributes=False).encode() + b"\0")
    return digest.hexdigest()


def docstring_reads(package_dir: Path = PACKAGE_DIR) -> list[str]:
    """Every place code in the package reads a docstring, as ``file:line``."""
    found = []
    for path in sorted(package_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            reads = (
                (isinstance(node, ast.Attribute) and node.attr in _DOC_READERS)
                or (isinstance(node, ast.Name) and node.id in _DOC_READERS)
                or (isinstance(node, ast.alias) and node.name.split(".")[-1] in _DOC_READERS)
                or (isinstance(node, ast.Constant) and node.value == "__doc__")
            )
            if reads:
                line = getattr(node, "lineno", 0)
                found.append(f"{path.relative_to(package_dir.parent)}:{line}")
    return found


_DOC_READERS = frozenset({"__doc__", "getdoc"})


def _path(name: str, package_dir: Path) -> Path:
    parts = name.split(".")[2:]  # past "tradeline.strategy"
    base = package_dir.joinpath(*parts)
    return base / "__init__.py" if base.is_dir() else base.with_suffix(".py")


def _parse(name: str, package_dir: Path) -> ast.Module:
    path = _path(name, package_dir)
    if not path.exists():
        raise ValueError(f"{name} is not a module of {PACKAGE} (no {path})")
    return ast.parse(path.read_text(), str(path))


def _imports(tree: ast.Module, name: str, package_dir: Path) -> list[str]:
    """The package's modules ``tree`` imports, and the packages that hold them."""
    is_package = _path(name, package_dir).name == "__init__.py"
    here = name if is_package else name.rpartition(".")[0]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = here.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([base] if base else []))
            found.append(base)
            # ``from tradeline.strategy import indicators`` imports a module by name.
            found.extend(f"{base}.{alias.name}" for alias in node.names)
    modules = set()
    for imported in found:
        if imported != PACKAGE and not imported.startswith(PACKAGE + "."):
            continue
        parts = imported.split(".")
        for end in range(2, len(parts) + 1):
            candidate = ".".join(parts[:end])
            if _path(candidate, package_dir).exists():
                modules.add(candidate)
    return sorted(modules)


def _without_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree
