"""Markdown helpers shared by every module's report.

The reports are the toolkit's deliverables, and two runs must write them
byte for byte the same, so every module lays out its tables through the same
function rather than its own copy.
"""

from __future__ import annotations

from collections.abc import Sequence


def table(
    headers: Sequence[str], rows: Sequence[Sequence[object]], right: Sequence[int] = ()
) -> str:
    """A GitHub-flavoured Markdown table; columns in ``right`` are right-aligned."""
    align = ["---:" if i in right else "---" for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)
