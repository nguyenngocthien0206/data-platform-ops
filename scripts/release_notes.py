"""Print one version's section of CHANGELOG.md, for the GitHub Release notes.

Used by the release workflow on a pushed ``v*`` tag, and by the tests to prove
that the changelog, ``pyproject.toml`` and the package agree on the version.
Exits non-zero when the changelog has no section for the version, so a tag
without release notes never publishes.

    python scripts/release_notes.py 1.0.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
# Keep a Changelog headings: "## [1.0.0] - 2026-09-29" or "## [Unreleased]".
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")
# Link definitions at the end of the file ("[1.0.0]: https://..."), not notes.
LINK = re.compile(r"^\[[^\]]+\]:\s")


def sections(text: str) -> dict[str, str]:
    """Each version's body, in file order, without its heading."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            current = found.setdefault(heading["version"], [])
        elif line.startswith("## "):
            current = None
        elif current is not None and not LINK.match(line):
            current.append(line)
    return {version: "\n".join(lines).strip() + "\n" for version, lines in found.items()}


def newest(text: str) -> str:
    """The first released version in the changelog."""
    released = [v for v in sections(text) if v.lower() != "unreleased"]
    if not released:
        raise ValueError("the changelog has no released version")
    return released[0]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: release_notes.py <version>", file=sys.stderr)
        return 2
    wanted = argv[0].removeprefix("v")
    body = sections(CHANGELOG.read_text(encoding="utf-8")).get(wanted)
    if body is None or not body.strip():
        print(f"error: CHANGELOG.md has no section for {wanted}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
