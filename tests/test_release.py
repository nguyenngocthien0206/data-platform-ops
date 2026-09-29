"""The changelog, pyproject.toml and the package agree on the version.

The release workflow publishes only when the pushed tag equals the pyproject
version and the changelog has notes for it; this keeps the three in step on
every pull request, long before anyone tags.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from platform_ops import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_notes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


release_notes = _load()


def test_pyproject_package_and_changelog_agree() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert pyproject["project"]["version"] == __version__
    assert release_notes.newest(changelog) == __version__


def test_the_current_version_has_release_notes(capsys: pytest.CaptureFixture[str]) -> None:
    assert release_notes.main([f"v{__version__}"]) == 0
    notes = capsys.readouterr().out
    assert notes.strip()
    assert not notes.startswith("## "), "the heading is the release title, not part of the notes"


def test_a_version_without_notes_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert release_notes.main(["9.9.9"]) == 1
    assert "no section for 9.9.9" in capsys.readouterr().err


def test_sections_stop_at_the_next_heading() -> None:
    text = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n- next\n\n"
        "## [1.1.0] - 2026-10-01\n\n### Added\n\n- b\n\n"
        "## [1.0.0] - 2026-09-29\n\n- a\n\n"
        "[1.0.0]: https://example.com\n"
    )
    found = release_notes.sections(text)
    assert list(found) == ["Unreleased", "1.1.0", "1.0.0"]
    assert found["1.1.0"] == "### Added\n\n- b\n"
    assert found["1.0.0"] == "- a\n", "link definitions at the end are not notes"
    assert release_notes.newest(text) == "1.1.0"
