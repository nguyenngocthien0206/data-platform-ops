"""The architecture diagram is generated, and its logos are accounted for (Phase 15).

``docs/images/architecture.svg`` is written by ``scripts/make_architecture.py``
from the dbt project and the config. These tests fail when the committed file
is stale, so a change to the project or the script cannot leave the README
showing an old picture, and when a logo is used or vendored without its source
in ``docs/images/logos/LICENSES.md``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "make_architecture.py"
LOGOS = REPO_ROOT / "docs" / "images" / "logos"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_architecture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_architecture"] = module
    spec.loader.exec_module(module)
    return module


architecture = _load()


def _licensed() -> set[str]:
    text = (LOGOS / "LICENSES.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([^`]+)` \|", text, flags=re.MULTILINE))


def test_the_committed_diagram_is_what_the_script_draws() -> None:
    committed = architecture.OUTPUT.read_text(encoding="utf-8")
    assert committed == architecture.render(), (
        "docs/images/architecture.svg is out of date: run "
        "`uv run python scripts/make_architecture.py` and commit the result"
    )


def test_its_numbers_come_from_the_project() -> None:
    facts = architecture.facts()
    assert facts["models"] == len(list((REPO_ROOT / "dbt" / "models").rglob("*.sql")))
    assert facts["tables"] > 0 and facts["reports"] > 0 and facts["exposures"] > 0
    svg = architecture.render()
    assert f"{facts['models']} models" in svg
    assert f"{facts['clean_nights']} clean nights" in svg


def test_every_logo_is_licensed_and_every_licensed_logo_is_vendored() -> None:
    vendored = {p.name for p in LOGOS.iterdir() if p.suffix in (".svg", ".png")}
    licensed = _licensed()
    assert vendored == licensed
    assert architecture.draw(architecture.facts()).logos <= licensed


def test_text_that_does_not_fit_fails_instead_of_overflowing() -> None:
    assert architecture.wrap("a b c", width=200, size=12, max_lines=1) == ["a b c"]
    with pytest.raises(ValueError, match="does not fit"):
        architecture.wrap("word " * 40, width=100, size=12, max_lines=2)


# -- the screenshots ---------------------------------------------------------------------


def _load_curate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "curate_screens", REPO_ROOT / "scripts" / "curate_screens.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["curate_screens"] = module
    spec.loader.exec_module(module)
    return module


curate_screens = _load_curate()


def test_the_screens_are_picked_cut_and_named(tmp_path: Path) -> None:
    from PIL import Image

    shots, out = tmp_path / "shots", tmp_path / "out"
    for screen in curate_screens.SCREENS:
        (shots / screen.source).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1440, 3000), "white").save(shots / screen.source)
    curate_screens.curate(shots, out)
    for screen in curate_screens.SCREENS:
        with Image.open(out / screen.name) as image:
            assert image.size == (1440, screen.max_height)


def test_a_missing_screen_names_the_target_that_makes_it(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="make docker-metabase-check"):
        curate_screens.curate(tmp_path, tmp_path / "out")
