"""The lineage graph: sources, models, tests and exposures, parent to child.

Two later modules lean on this. Cost attribution uses it so that a table feeding
a used table is never called unused. Incident grouping uses it to find the root
cause of a cascade of failures, and to weigh how much of the business a failure
reaches. Both need answers that are identical from run to run, so every helper
that could return one of several nodes breaks ties by unique id.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import duckdb
import networkx as nx

from platform_ops.common.config import Tier, TierWeights
from platform_ops.common.db import OPS_SCHEMA, transaction
from platform_ops.metadata.manifest import Node

CONSUMER_TYPES: frozenset[str] = frozenset({"model", "exposure"})


def build_graph(nodes: Mapping[str, Node]) -> nx.DiGraph[str]:
    """A directed graph with an edge from each parent to each child.

    Parents that are not in ``nodes`` (for example a macro or a disabled node)
    are ignored rather than added as unlabelled vertices.
    """
    graph: nx.DiGraph[str] = nx.DiGraph()
    for node in nodes.values():
        graph.add_node(node.unique_id, resource_type=node.resource_type, name=node.name)
    for node in nodes.values():
        for parent in node.depends_on:
            if parent in nodes:
                graph.add_edge(parent, node.unique_id)
    return graph


def upstream(graph: nx.DiGraph[str], node: str) -> set[str]:
    """Every ancestor of ``node``."""
    return set(nx.ancestors(graph, node))


def downstream(graph: nx.DiGraph[str], node: str) -> set[str]:
    """Every descendant of ``node``."""
    return set(nx.descendants(graph, node))


def nearest_failed_ancestor(graph: nx.DiGraph[str], node: str, failed: Iterable[str]) -> str | None:
    """The closest failed ancestor of ``node``, or ``None`` if none failed.

    Closest means fewest edges upstream. When several failed ancestors are
    equally close, as happens in a diamond, the smallest unique id wins, so the
    same failures always attach to the same root.
    """
    failed_set = set(failed)
    seen = {node}
    frontier = {node}
    while frontier:
        parents = {p for child in frontier for p in graph.predecessors(child)} - seen
        hits = parents & failed_set
        if hits:
            return min(hits)
        seen |= parents
        frontier = parents
    return None


@dataclass(frozen=True)
class Consumer:
    unique_id: str
    resource_type: str
    tier: Tier
    weight: int


def downstream_consumers(
    graph: nx.DiGraph[str],
    node: str,
    tier_of: Mapping[str, Tier],
    weights: TierWeights,
    *,
    default_tier: Tier = "best_effort",
) -> list[Consumer]:
    """Downstream models and exposures, each weighted by its tier.

    Tests are not consumers: a failing dataset does not break a test's
    audience. A consumer missing from ``tier_of`` counts as ``default_tier``.
    """
    consumers: list[Consumer] = []
    for unique_id in sorted(downstream(graph, node)):
        resource_type = graph.nodes[unique_id]["resource_type"]
        if resource_type not in CONSUMER_TYPES:
            continue
        tier = tier_of.get(unique_id, default_tier)
        consumers.append(Consumer(unique_id, resource_type, tier, weights.weight(tier)))
    return consumers


def consumer_weight(consumers: Iterable[Consumer]) -> int:
    return sum(consumer.weight for consumer in consumers)


def edge_rows(graph: nx.DiGraph[str]) -> list[tuple[str, str, str, str]]:
    return sorted(
        (
            parent,
            child,
            graph.nodes[parent]["resource_type"],
            graph.nodes[child]["resource_type"],
        )
        for parent, child in graph.edges
    )


def persist_edges(connection: duckdb.DuckDBPyConnection, graph: nx.DiGraph[str]) -> int:
    """Replace ``ops.lineage_edges`` with the edges of ``graph``.

    Replaced, never appended, so the table cannot mix edges from an older
    manifest with the current one. The replace and every insert share one
    transaction: a reader never sees an empty or half-written table, and the
    write-ahead log is flushed once instead of once per row.
    """
    rows = edge_rows(graph)
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA}")
    with transaction(connection):
        connection.execute(
            f"""CREATE OR REPLACE TABLE {OPS_SCHEMA}.lineage_edges (
                    parent_id VARCHAR NOT NULL,
                    child_id VARCHAR NOT NULL,
                    parent_type VARCHAR NOT NULL,
                    child_type VARCHAR NOT NULL
                )"""
        )
        if rows:
            connection.executemany(
                f"INSERT INTO {OPS_SCHEMA}.lineage_edges VALUES (?, ?, ?, ?)", rows
            )
    return len(rows)
