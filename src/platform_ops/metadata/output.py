"""How the ownership check reports, for the people and tools that read it.

Plain text for a CI log, annotations that GitHub Actions shows on the lines of
a pull request, and a JSON summary for recording a baseline before the check
is made required. Every finding points at the file to change: the dataset's own
SQL or YAML, or the rule's line in ``ownership.yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_ops.metadata.check import CheckReport, Finding
from platform_ops.metadata.manifest import Node


@dataclass(frozen=True)
class Locations:
    """Finds the file and line a finding is about, relative to the repository."""

    nodes: dict[str, Node]
    ownership_file: Path
    project_dir: Path | None = None
    root_project: str = ""
    repo_root: Path | None = None

    def for_finding(self, finding: Finding) -> tuple[str, int] | None:
        if finding.node is not None:
            found = self.of_node(finding.node)
            if found is not None:
                return found
        if finding.rule is not None:
            return self.of_rule(finding.rule)
        return None

    def of_node(self, unique_id: str) -> tuple[str, int] | None:
        """The dataset's file in the root project; ``None`` for an installed package's."""
        node = self.nodes.get(unique_id)
        if node is None or not node.path or self.project_dir is None:
            return None
        if self.root_project and node.package != self.root_project:
            return None
        path = self.project_dir / node.path
        line = 1
        if path.suffix in (".yml", ".yaml"):
            pattern = re.compile(rf"\bname:\s*[\"']?{re.escape(node.name)}[\"']?\s*$")
            line = _first_line(path, pattern.search) or 1
        return self._relative(path), line

    def of_rule(self, match: str) -> tuple[str, int]:
        line = _first_line(self.ownership_file, lambda text: "match" in text and match in text)
        return self._relative(self.ownership_file), line or 1

    def _relative(self, path: Path) -> str:
        if self.repo_root is not None:
            try:
                return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
            except ValueError:
                pass
        return path.as_posix()


def _first_line(path: Path, matches: Callable[[str], object]) -> int | None:
    if not path.is_file():
        return None
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if matches(text):
            return number
    return None


def coverage_lines(report: CheckReport) -> list[str]:
    """Owned out of total per resource type; types the project does not use are left out."""
    return [
        f"{resource_type + 's':<10} {owned:>4} of {total:<4} owned"
        for resource_type, (owned, total) in report.coverage.items()
        if total
    ]


def text_lines(findings: list[Finding], level: str, locations: Locations) -> list[str]:
    lines: list[str] = []
    indent = " " * (len(level) + 2)
    for finding in findings:
        where = locations.for_finding(finding)
        suffix = f" [{where[0]}:{where[1]}]" if where else ""
        lines.append(f"{level}: {finding.message}{suffix}")
        if finding.fix:
            lines.append(f"{indent}fix: {finding.fix}")
    return lines


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def github_lines(findings: list[Finding], level: str, locations: Locations) -> list[str]:
    """``::error file=...,line=...::message`` workflow commands, one per finding."""
    lines: list[str] = []
    for finding in findings:
        properties = []
        where = locations.for_finding(finding)
        if where is not None:
            properties += [f"file={_escape_property(where[0])}", f"line={where[1]}"]
        properties.append("title=Ownership check")
        message = finding.message + (f". Fix: {finding.fix}" if finding.fix else "")
        lines.append(f"::{level} {','.join(properties)}::{_escape_data(message)}")
    return lines


def summary(report: CheckReport, *, manifest: Path, report_only: bool) -> dict[str, Any]:
    """What a baseline needs: coverage per type, the share owned, and every finding."""
    owned = sum(owned for owned, _ in report.coverage.values())
    total = sum(total for _, total in report.coverage.values())
    return {
        "manifest": manifest.as_posix(),
        "passed": report.passed,
        "report_only": report_only,
        "coverage": {
            resource_type: {"owned": done, "total": count}
            for resource_type, (done, count) in report.coverage.items()
        },
        "owned": owned,
        "total": total,
        "owned_share": round(owned / total, 4) if total else 1.0,
        "errors": [_finding(f) for f in report.error_findings],
        "warnings": [_finding(f) for f in report.warning_findings],
    }


def _finding(finding: Finding) -> dict[str, Any]:
    return {
        "message": finding.message,
        "node": finding.node,
        "rule": finding.rule,
        "fix": finding.fix,
    }
