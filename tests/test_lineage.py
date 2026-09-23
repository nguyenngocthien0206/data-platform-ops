"""Lineage helpers on a small hand-built graph (a SPEC Phase 1 acceptance item).

Edges, parent to child:

    raw_a -> stg_a        stg_a -> int_x        int_x -> mart     mart -> board (exposure)
    raw_b -> stg_b        stg_b -> int_x        int_y -> mart     mart -> unique_mart (test)
                          stg_a -> int_y        int_x -> dead_end (abandoned, no children)

Two diamonds: stg_a and stg_b both reach mart through int_x, and stg_a reaches
mart through both int_x and int_y.
"""

from __future__ import annotations

import pytest

from platform_ops.common.config import TierWeights
from platform_ops.common.db import connect
from platform_ops.metadata.lineage import (
    build_graph,
    consumer_weight,
    downstream,
    downstream_consumers,
    edge_rows,
    nearest_failed_ancestor,
    persist_edges,
    upstream,
)
from platform_ops.metadata.manifest import Node

RAW_A, RAW_B = "source.company.raw.a", "source.company.raw.b"
STG_A, STG_B = "model.company.stg_a", "model.company.stg_b"
INT_X, INT_Y = "model.company.int_x", "model.company.int_y"
MART, DEAD = "model.company.mart", "model.company.dead_end"
BOARD, TEST = "exposure.company.board", "test.company.unique_mart"

WEIGHTS = TierWeights(critical=3, important=2, best_effort=1)


def _node(unique_id: str, *parents: str) -> Node:
    resource_type = unique_id.split(".", 1)[0]
    return Node(
        unique_id=unique_id,
        resource_type=resource_type,
        name=unique_id.rsplit(".", 1)[1],
        depends_on=tuple(parents),
    )


@pytest.fixture
def nodes() -> dict[str, Node]:
    built = [
        _node(RAW_A),
        _node(RAW_B),
        _node(STG_A, RAW_A),
        _node(STG_B, RAW_B),
        _node(INT_X, STG_A, STG_B),
        _node(INT_Y, STG_A),
        _node(MART, INT_X, INT_Y),
        _node(DEAD, INT_X),
        _node(BOARD, MART),
        _node(TEST, MART),
    ]
    return {node.unique_id: node for node in built}


def test_edges_point_from_parent_to_child(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    assert graph.has_edge(STG_A, INT_X)
    assert not graph.has_edge(INT_X, STG_A)
    assert graph.number_of_edges() == 10


def test_parents_outside_the_manifest_are_ignored(nodes: dict[str, Node]) -> None:
    nodes[MART] = _node(MART, INT_X, INT_Y, "macro.company.something")
    graph = build_graph(nodes)
    assert "macro.company.something" not in graph


def test_upstream_and_downstream(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    assert upstream(graph, MART) == {RAW_A, RAW_B, STG_A, STG_B, INT_X, INT_Y}
    assert downstream(graph, STG_B) == {INT_X, MART, DEAD, BOARD, TEST}
    assert downstream(graph, DEAD) == set()
    assert upstream(graph, RAW_A) == set()


def test_nearest_failed_ancestor_prefers_the_closest(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    assert nearest_failed_ancestor(graph, MART, {INT_X, STG_A}) == INT_X


def test_diamond_tie_breaks_on_the_smallest_unique_id(nodes: dict[str, Node]) -> None:
    """stg_a and stg_b are both two edges above mart. The answer must not
    depend on set iteration order, or Phase 3 would open different incidents
    for the same failures on different runs."""
    graph = build_graph(nodes)
    for failed in ({STG_A, STG_B}, {STG_B, STG_A}):
        assert nearest_failed_ancestor(graph, MART, failed) == STG_A


def test_one_ancestor_reached_by_two_paths_is_found_once(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    assert nearest_failed_ancestor(graph, MART, {STG_A}) == STG_A


def test_no_failed_ancestor_returns_none(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    assert nearest_failed_ancestor(graph, MART, set()) is None
    assert nearest_failed_ancestor(graph, MART, {DEAD, BOARD}) is None  # not ancestors


def test_the_node_itself_is_not_its_own_ancestor(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    assert nearest_failed_ancestor(graph, INT_X, {INT_X}) is None


def test_consumers_exclude_tests_and_are_weighted_by_tier(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    tiers = {MART: "critical", BOARD: "critical", DEAD: "best_effort"}
    consumers = downstream_consumers(graph, INT_X, tiers, WEIGHTS, default_tier="important")  # type: ignore[arg-type]
    assert [c.unique_id for c in consumers] == [BOARD, DEAD, MART]
    assert [c.weight for c in consumers] == [3, 1, 3]
    assert consumer_weight(consumers) == 7


def test_consumers_default_tier_applies_to_unknown_nodes(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    consumers = downstream_consumers(graph, MART, {}, WEIGHTS)
    assert [(c.unique_id, c.tier, c.weight) for c in consumers] == [(BOARD, "best_effort", 1)]


def test_edges_persist_and_replace(nodes: dict[str, Node]) -> None:
    graph = build_graph(nodes)
    connection = connect(path=":memory:")
    assert persist_edges(connection, graph) == 10
    assert persist_edges(connection, graph) == 10, "a second persist replaces, not appends"
    stored = connection.execute("SELECT * FROM ops.lineage_edges ORDER BY ALL").fetchall()
    assert stored == edge_rows(graph)
    assert (MART, BOARD, "model", "exposure") in stored
