"""Every relative link in the Markdown docs resolves, anchors included (Phase 15).

A reader who follows the README from a fresh clone should never hit a dead
link: to a file, to an image, or to a heading in another file. Links to the
web are not followed (the suite runs offline). Anchors are matched against the
target's headings the way GitHub builds them: lower case, punctuation dropped,
spaces turned into hyphens, a repeated heading numbered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP = {".git", ".venv", "node_modules", "dbt_packages", "target", "logs", "reports", "data"}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)|<img[^>]+src=\"([^\"]+)\"")
FENCE = re.compile(r"^(```|~~~)")


def _markdown_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.md")
        if not SKIP.intersection(p.relative_to(REPO_ROOT).parts) and ".pytest" not in str(p)
    )


def _prose(path: Path) -> list[str]:
    """The file's lines outside fenced code blocks."""
    lines, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]|<[^>]+>", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for line in _prose(path):
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if not match:
            continue
        base = slug(match.group(1))
        n = counts.get(base, 0)
        counts[base] = n + 1
        found.add(base if n == 0 else f"{base}-{n}")
    return found


def broken_links(path: Path) -> list[str]:
    problems = []
    for line in _prose(path):
        for match in LINK.finditer(line):
            target = match.group(1) or match.group(2)
            if re.match(r"^[a-z]+:", target) or target.startswith("//"):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            if not resolved.exists():
                problems.append(f"{target}: no such file")
            elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
                problems.append(f"{target}: no heading #{anchor} in {resolved.name}")
    return problems


@pytest.mark.parametrize(
    "path", _markdown_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_every_relative_link_resolves(path: Path) -> None:
    assert broken_links(path) == []


def test_the_checker_catches_a_dead_file_and_a_dead_anchor(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Title\n\n## Adopting it\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](other.md#adopting-it) [gone](missing.md) [bad](other.md#nowhere) "
        "[web](https://example.com)\n```\n[in code](missing-too.md)\n```\n",
        encoding="utf-8",
    )
    assert broken_links(page) == [
        "missing.md: no such file",
        "other.md#nowhere: no heading #nowhere in other.md",
    ]


def test_headings_slug_like_github() -> None:
    assert slug("Adopting it") == "adopting-it"
    assert slug("Ownership registry (`registry.py`, `check.py`)") == (
        "ownership-registry-registrypy-checkpy"
    )
    assert slug("01  THE BILL") == "01--the-bill"
