"""Group one run's failing checks into root-cause incidents.

The rules, in order (ADR 0005):

1. A node has failed in a run if any check about it failed.
2. A failed node is a root if none of its ancestors failed. Every other failed
   node belongs to the root reached by following nearest failed ancestors up the
   lineage graph. Ties go to the smallest unique id, so a diamond always
   resolves the same way. A failed node below more than one root (a model
   reading two broken feeds) is also shared with every other root above it, so
   each of their incidents shows it; its checks are counted once, under the
   root the tie-break chose.
3. One incident per root. Five checks failing on one node are one incident.
4. A skipped node is impact of the root above it, never an alert of its own.
5. A root that already has an open incident appends to it instead of opening a
   second one; the same broken table failing again the next night is the same
   problem. The desk applies this rule against the store (``desk``).

Everything here is pure: a graph and events in, groups out. The desk does the
reading and writing around it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import networkx as nx

from platform_ops.incidents.ingest import CheckEvent
from platform_ops.metadata.lineage import nearest_failed_ancestor, upstream


@dataclass(frozen=True)
class Group:
    """The failures in one run that share a root cause."""

    root: str
    events: tuple[CheckEvent, ...]
    failed_nodes: tuple[str, ...]
    skipped: tuple[str, ...]
    # Failed nodes below this root and below another root too, wherever counted.
    shared: tuple[str, ...] = ()


def roots_by_node(graph: nx.DiGraph[str], failed: Iterable[str]) -> dict[str, str]:
    """Map every failed node to its root."""
    failed_set = set(failed)
    root_of: dict[str, str] = {}

    def resolve(node: str) -> str:
        if node in root_of:
            return root_of[node]
        parent = nearest_failed_ancestor(graph, node, failed_set) if node in graph else None
        root_of[node] = node if parent is None else resolve(parent)
        return root_of[node]

    for node in sorted(failed_set):
        resolve(node)
    return root_of


def group_failures(
    graph: nx.DiGraph[str], events: Iterable[CheckEvent], skipped: Iterable[str] = ()
) -> list[Group]:
    """One group per root, ordered by root id."""
    events = sorted(events)
    root_of = roots_by_node(graph, {event.subject for event in events})
    failed = set(root_of)

    events_by_root: dict[str, list[CheckEvent]] = {}
    for event in events:
        events_by_root.setdefault(root_of[event.subject], []).append(event)

    skipped_by_root: dict[str, set[str]] = {}
    for node in skipped:
        if node in failed or node not in graph:
            continue
        parent = nearest_failed_ancestor(graph, node, failed)
        if parent is not None:
            skipped_by_root.setdefault(root_of[parent], set()).add(node)

    shared = shared_failures(graph, root_of)
    return [
        Group(
            root=root,
            events=tuple(events_by_root[root]),
            failed_nodes=tuple(sorted({e.subject for e in events_by_root[root]})),
            skipped=tuple(sorted(skipped_by_root.get(root, ()))),
            shared=tuple(sorted(n for n, roots in shared.items() if root in roots)),
        )
        for root in sorted(events_by_root)
    ]


def shared_failures(graph: nx.DiGraph[str], root_of: dict[str, str]) -> dict[str, frozenset[str]]:
    """Failed nodes below more than one root, and every root above each of them."""
    roots = set(root_of.values())
    found: dict[str, frozenset[str]] = {}
    for node in sorted(root_of):
        if node in roots or node not in graph:
            continue
        above = frozenset(roots & upstream(graph, node))
        if len(above) > 1:
            found[node] = above
    return found
