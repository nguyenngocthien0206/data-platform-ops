"""The README check: numbers in a results section must come from a report."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_readme.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readme", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_readme"] = module
    spec.loader.exec_module(module)
    return module


check_readme = _load()


def test_numbers_are_found_whatever_their_formatting() -> None:
    text = "Cost $296.02 for 35,813 queries, 94.968% matched, 8 of 8 right."
    assert check_readme.numbers(text) == ["$296.02", "35,813", "94.968%", "8", "8"]
    assert [check_readme.normalize(n) for n in check_readme.numbers(text)] == [
        "296.02", "35813", "94.968", "8", "8",
    ]  # fmt: skip


def test_code_links_and_adr_numbers_are_not_data() -> None:
    text = "Run `make demo` at scale 1.0 (ADR 0005 and 0009), see [x](docs/adr/0009-a.md), 7 rows."
    assert check_readme.numbers(text) == ["7"]


def test_only_results_sections_are_checked() -> None:
    markdown = "# Title\n42\n## Results at scale 1.0\n7 rows\n### Detail\n9\n## Running it\n11\n"
    assert [line for _, line in check_readme.results_lines(markdown)] == [
        "7 rows", "### Detail", "9",
    ]  # fmt: skip


def test_missing_numbers_are_reported_with_their_line(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "cost.md").write_text("| total | $296.02 |\n| queries | 35,813 |\n")
    readme = tmp_path / "README.md"
    readme.write_text(
        "## Results\n"
        "Priced 35,813 queries for $296.02.\n"
        "Saved 12.5% overall.\n"
        "The demo took 6 minutes 54 seconds. <!-- readme-check: runtime -->\n"
    )
    missing, checked = check_readme.check(readme, ["cost.md"], reports)
    assert checked == 3
    assert [(m.line, m.number) for m in missing] == [(3, "12.5%")]
