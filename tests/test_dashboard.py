"""The dashboards: an ops-only data layer, and every page rendering on real data.

One warehouse is built for the whole module the way ``make demo`` builds it,
at scale 0.01: simulate three weeks, price them, run the incident scenario and
the reconciliation (with DuckDB as the legacy engine, so no Docker is needed).
Every page is then rendered with Streamlit's AppTest.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings
from platform_ops.cost.sql_parse import parse_query
from platform_ops.dashboard.queries import QUERIES, MissingData, Warehouse, producer

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGES = REPO_ROOT / "dashboards" / "pages"
runner = CliRunner()


# -- the data layer, no warehouse needed -----------------------------------------------


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_every_query_reads_only_ops(name: str) -> None:
    query = QUERIES[name]
    parsed = parse_query(query.sql, {})
    assert parsed.ok, parsed.error
    assert parsed.writes is None
    read = set(parsed.reads)
    assert read, "a query must read something"
    assert all(table.startswith("ops.") for table in read), read
    assert {t.removeprefix("ops.") for t in read} == set(query.tables), "declared tables"


def test_every_table_names_the_command_that_produces_it() -> None:
    for query in QUERIES.values():
        for table in query.tables:
            assert producer(table) != "make demo", f"{table} has no producing command"


def test_a_missing_warehouse_says_what_to_run(tmp_path: Path) -> None:
    with pytest.raises(MissingData, match="make demo"):
        Warehouse(tmp_path / "absent.duckdb").frame("showback")


def test_a_missing_table_names_its_command(tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).execute("CREATE SCHEMA ops").close()
    with pytest.raises(MissingData, match="make incidents"):
        Warehouse(path).frame("incidents")


# -- a real warehouse ------------------------------------------------------------------


@pytest.fixture(scope="module")
def built(
    tmp_path_factory: pytest.TempPathFactory, isolated_config: Callable[..., Path]
) -> Iterator[Settings]:
    settings_path = isolated_config(tmp_path_factory.mktemp("dashboard"), 0.01, weeks=3)
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    try:
        for command in (["simulation", "run"], ["cost", "report"], ["incidents", "run"],
                        ["reconcile", "run", "--engine", "duckdb"]):  # fmt: skip
            result = runner.invoke(app, command)
            assert result.exit_code == 0, f"{' '.join(command)} failed:\n{result.output}"
        yield load_settings(settings_path)
    finally:
        patch.undo()


# Hotspots need tables over `hotspot_min_table_bytes` (20 MB); none are that big at 0.01.
MAY_BE_EMPTY_AT_SMALL_SCALE = {"hotspots"}


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(QUERIES))
def test_every_query_runs_on_a_full_warehouse(built: Settings, name: str) -> None:
    frame = Warehouse(built.resolve(built.paths.duckdb)).frame(name)
    if name not in MAY_BE_EMPTY_AT_SMALL_SCALE:
        assert not frame.empty, f"{name} returned nothing on a full run"


def _elements(node: Any, found: Counter[str]) -> Counter[str]:
    children = getattr(node, "children", None) or {}
    for child in children.values() if isinstance(children, dict) else children:
        found[str(getattr(child, "type", type(child).__name__))] += 1
        _elements(child, found)
    return found


EXPECTED = {
    "overview": ("Data platform operations", 1),
    "cost": ("Cost attribution", 3),
    "incidents": ("Incident management", 4),
    "reconciliation": ("Migration reconciliation", 1),
}


@pytest.mark.integration
@pytest.mark.parametrize("page", sorted(EXPECTED))
def test_every_page_renders(built: Settings, page: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = built.root / "config" / "settings.yaml"
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config))
    title, charts = EXPECTED[page]
    test = AppTest.from_file(str(PAGES / f"{page}.py"), default_timeout=120)
    test.run()
    assert not test.exception, [e.value for e in test.exception]
    assert [t.value for t in test.title] == [title]
    assert not test.info, [i.value for i in test.info]  # no "run make X first" on a full run
    elements = _elements(test._tree, Counter())
    assert elements["vega_lite_chart"] == charts
    assert elements["metric"] >= 1


@pytest.mark.integration
def test_the_cost_page_switches_pricing_model(
    built: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(built.root / "config" / "settings.yaml"))
    test = AppTest.from_file(str(PAGES / "cost.py"), default_timeout=120).run()
    compute_total = test.metric[0].value
    test.button_group[0].set_value("Scan").run()
    assert not test.exception
    assert test.metric[0].value != compute_total
