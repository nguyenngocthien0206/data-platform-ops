"""Structural invariants of the dbt project, checked on a `dbt parse` manifest.

Parsing needs no data and no warehouse, so these run fast and in CI. They pin
the properties Phases 2 and 3 rely on: abandoned models really are dead ends,
every live model reaches a dashboard, and every dataset has exactly one owner.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import networkx as nx
import pytest
import yaml

from platform_ops.common.config import load_settings
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.metadata.check import run_check
from platform_ops.metadata.lineage import build_graph, downstream
from platform_ops.metadata.manifest import Node, load_manifest
from platform_ops.metadata.registry import Registry

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
BUSINESS_TEAMS = ("sales", "marketing", "finance", "product")


@pytest.fixture(scope="module")
def parsed(
    tmp_path_factory: pytest.TempPathFactory,
    isolated_config: Callable[[Path, float], Path],
) -> tuple[dict[str, Node], nx.DiGraph, Path]:
    root = tmp_path_factory.mktemp("parse")
    settings = load_settings(isolated_config(root, 0.01))
    invocation = invocation_from_settings(settings)
    run_dbt(invocation, ["parse"])
    nodes = load_manifest(invocation.manifest_path)
    return nodes, build_graph(nodes), root / "config"


def _of_type(nodes: dict[str, Node], resource_type: str) -> list[Node]:
    return [n for n in nodes.values() if n.resource_type == resource_type]


def _abandoned(nodes: dict[str, Node]) -> list[Node]:
    return [n for n in _of_type(nodes, "model") if n.folder == "abandoned"]


def test_model_count_is_around_120(parsed: tuple[dict[str, Node], nx.DiGraph, Path]) -> None:
    nodes, _, _ = parsed
    assert 110 <= len(_of_type(nodes, "model")) <= 130


def test_there_are_10_to_15_abandoned_models(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    nodes, _, _ = parsed
    assert 10 <= len(_abandoned(nodes)) <= 15


def test_every_abandoned_model_has_no_downstream_nodes_and_no_exposures(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    """SPEC Phase 1 acceptance. Also rules out dbt tests on abandoned models,
    since a test is a downstream node and its query would count as a read."""
    nodes, graph, _ = parsed
    for model in _abandoned(nodes):
        below = downstream(graph, model.unique_id)
        assert below == set(), f"{model.name} feeds {sorted(below)}"
        exposures = [e for e in _of_type(nodes, "exposure") if model.unique_id in e.depends_on]
        assert exposures == [], f"{model.name} is used by {exposures}"


def test_every_live_model_reaches_an_exposure(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    """Keeps Phase 2's unused list exactly equal to the abandoned set."""
    nodes, graph, _ = parsed
    for model in _of_type(nodes, "model"):
        if model.folder == "abandoned":
            continue
        consumers = {
            n for n in downstream(graph, model.unique_id) if nodes[n].resource_type == "exposure"
        }
        assert consumers, f"{model.name} reaches no dashboard"


def test_exposures_are_12_dashboards_spread_over_the_business_teams(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    nodes, _, _ = parsed
    exposures = _of_type(nodes, "exposure")
    assert len(exposures) == 12
    for team in BUSINESS_TEAMS:
        assert sum(e.name.startswith(f"{team}_") for e in exposures) == 3, team


def test_marts_and_abandoned_models_carry_a_team_prefix(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    nodes, _, _ = parsed
    for model in _of_type(nodes, "model"):
        if model.folder in ("marts", "abandoned"):
            assert model.name.split("_", 1)[0] in BUSINESS_TEAMS, model.name
        else:
            assert model.name.startswith(("stg_", "int_")), model.name


def test_staging_has_one_model_per_raw_table(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    nodes, _, _ = parsed
    sources = {n.name for n in _of_type(nodes, "source")}
    staging = {
        n.name.removeprefix("stg_") for n in _of_type(nodes, "model") if n.folder == "staging"
    }
    assert staging == sources


def test_real_registry_owns_everything_with_no_warnings(
    parsed: tuple[dict[str, Node], nx.DiGraph, Path],
) -> None:
    nodes, _, config_dir = parsed
    report = run_check(Registry.from_config_dir(config_dir), nodes)
    assert report.errors == []
    assert report.warnings == []


def test_query_comment_is_appended_json_with_the_unique_id() -> None:
    project = yaml.safe_load((REPO_ROOT / "dbt" / "dbt_project.yml").read_text(encoding="utf-8"))
    comment = project["query-comment"]
    assert comment["append"] is True
    assert "unique_id" in comment["comment"] and "tojson" in comment["comment"]


def test_usage_tracking_is_off() -> None:
    project = yaml.safe_load((REPO_ROOT / "dbt" / "dbt_project.yml").read_text(encoding="utf-8"))
    assert project["flags"]["send_anonymous_usage_stats"] is False
