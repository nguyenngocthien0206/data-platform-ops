"""Check that every number in the READMEs' results comes from a generated report.

Usage (after ``make demo``, from the repo root)::

    uv run python scripts/check_readme.py        # or: make readme-check

For the root ``README.md`` and each ``src/platform_ops/<module>/README.md``,
every number in a section whose heading starts with "Results" must appear in
that module's reports under ``reports/`` (all reports, for the root README).
Numbers are compared as written, ignoring thousands separators, currency and
percent signs: ``$296.02``, ``35,813`` and ``94.968%`` must appear as
``296.02``, ``35813`` and ``94.968`` somewhere in the reports.

Some true numbers are not in any report: how long a command took is printed on
the console, not written to a file. A line that states one ends with
``<!-- readme-check: runtime -->`` and is skipped, so the exception is visible
in the README source rather than hidden in this script.

Exit status 1 lists every number that could not be found, with file and line.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORTS = REPO / "reports"
SKIP_MARKER = "<!-- readme-check: runtime -->"

# README -> the report files (globs under reports/) its numbers must come from.
SOURCES: dict[str, tuple[str, ...]] = {
    "README.md": ("*.md", "postmortems/*.md"),
    "src/platform_ops/cost/README.md": ("cost.md", "cost_proxy_accuracy.md"),
    "src/platform_ops/incidents/README.md": ("incidents.md", "postmortems/*.md"),
    "src/platform_ops/reconcile/README.md": ("reconciliation.md",),
}

NUMBER = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?(?![\w])")
# Not data: inline code, link targets, ADR numbers, and dates in headings.
NOISE = [
    re.compile(r"`[^`]*`"),
    re.compile(r"\]\([^)]*\)"),
    re.compile(r"\bADRs? \d{4}(?:(?:, | and | to )\d{4})*"),
    # The scale factor is a setting the run was made with, not a result of it.
    re.compile(r"\bscale(?: factor)? \d+(?:\.\d+)?"),
]


@dataclass(frozen=True)
class Missing:
    readme: str
    line: int
    number: str


def normalize(token: str) -> str:
    return token.replace("$", "").replace(",", "").replace("%", "")


def numbers(text: str) -> list[str]:
    for pattern in NOISE:
        text = pattern.sub(" ", text)
    return [m.group(0).rstrip(",") for m in NUMBER.finditer(text)]


def results_lines(markdown: str) -> list[tuple[int, str]]:
    """(line number, text) for every line inside a "Results" section."""
    inside = False
    level = 0
    found = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        heading = re.match(r"^(#+)\s+(.*)", line)
        if heading:
            depth = len(heading.group(1))
            if heading.group(2).strip().lower().startswith("results"):
                inside, level = True, depth
                continue
            if inside and depth <= level:
                inside = False
        if inside:
            found.append((number, line))
    return found


def report_numbers(patterns: Iterable[str], reports: Path) -> set[str]:
    values: set[str] = set()
    for pattern in patterns:
        for path in sorted(reports.glob(pattern)):
            values.update(normalize(n) for n in numbers(path.read_text(encoding="utf-8")))
    return values


def check(readme: Path, patterns: Iterable[str], reports: Path) -> tuple[list[Missing], int]:
    known = report_numbers(patterns, reports)
    missing: list[Missing] = []
    checked = 0
    for line_number, line in results_lines(readme.read_text(encoding="utf-8")):
        if SKIP_MARKER in line:
            continue
        for token in numbers(line):
            checked += 1
            if normalize(token) not in known:
                missing.append(Missing(readme.name, line_number, token))
    return missing, checked


def main() -> int:
    if not any(REPORTS.glob("*.md")):
        print("No reports found. Run `make demo` first.", file=sys.stderr)
        return 1
    failures: list[tuple[str, Missing]] = []
    for relative, patterns in SOURCES.items():
        readme = REPO / relative
        if not readme.is_file():
            continue
        missing, checked = check(readme, patterns, REPORTS)
        print(f"{relative}: {checked} numbers checked, {len(missing)} not found in reports")
        failures += [(relative, m) for m in missing]
    for relative, m in failures:
        print(f"  {relative}:{m.line}: {m.number}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
