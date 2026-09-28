"""The package boundaries, checked on every import in the source tree.

The three modules (cost, incidents, reconcile) share the metadata layer and
the simulated company, and never reach into each other: that is what lets one
be replaced or run without the others (ADR 0010). Imports inside functions
count too, since they are how boundaries usually get crossed quietly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "platform_ops"

# Package -> the platform_ops packages it may import (itself is always allowed).
ALLOWED: dict[str, set[str]] = {
    "common": set(),
    "metadata": {"common"},
    "simulation": {"common", "metadata"},
    "cost": {"common", "metadata"},
    "incidents": {"common", "metadata", "simulation"},
    "reconcile": {"common", "metadata", "simulation"},
    "dashboard": {"common", "metadata"},
}

# The one accepted exception: the simulated warehouse carries the cost module's
# collection instruments, the way a real warehouse carries a query history
# (docs/review/phase-6-review.md).
EXCEPTIONS: dict[str, set[str]] = {
    "simulation": {"cost.collect", "cost.growth", "cost.sizes", "cost.schema"},
}


def _imports(path: Path) -> set[str]:
    """``platform_ops`` modules imported by ``path``, without the package prefix."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "platform_ops" or node.module.startswith("platform_ops."):
                base = node.module.removeprefix("platform_ops").lstrip(".")
                found.update(f"{base}.{a.name}".lstrip(".") for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(
                a.name.removeprefix("platform_ops.")
                for a in node.names
                if a.name.startswith("platform_ops.")
            )
    return found


def _violations(package: str) -> list[str]:
    problems = []
    for path in sorted((SRC / package).rglob("*.py")):
        for target in sorted(_imports(path)):
            top = target.split(".", 1)[0]
            if top in {package, "__version__", ""} or top in ALLOWED[package]:
                continue
            if any(target == e or target.startswith(f"{e}.") for e in EXCEPTIONS.get(package, ())):
                continue
            problems.append(f"{path.relative_to(SRC)} imports platform_ops.{target}")
    return problems


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_packages_import_only_what_they_are_allowed_to(package: str) -> None:
    assert _violations(package) == []


def test_every_package_is_covered() -> None:
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert packages == set(ALLOWED)


def test_the_checker_catches_a_cross_module_import(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from platform_ops.cost.report import usd\n"
        "def f():\n    from platform_ops.reconcile import run\n"
    )
    assert _imports(sample) == {"cost.report.usd", "reconcile.run"}
