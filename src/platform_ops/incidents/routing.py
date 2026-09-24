"""Who gets paged for an incident.

The page goes to the owner of the incident's root, with one exception: when
the root is a staging model that reads a single source, it goes to the owner of
that source. Staging models only rename and cast (a Phase 1 convention), so a
staging failure almost always means the data arrived broken, and the person who
can fix that is whoever owns the feed, not whoever wrote the rename.

``naive_route`` is the rule without the exception. The report grades both, so
the value of the exception is measured rather than claimed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from platform_ops.incidents.severity import dataset_of
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import Registry

UNOWNED = "unowned"
STAGING_PREFIX = "stg_"


@dataclass(frozen=True)
class Route:
    owner: str
    team: str
    # The node whose ownership rule decided the page.
    routed_via: str


def _route_to(dataset: str, registry: Registry) -> Route:
    rule = registry.resolve(dataset).rule
    if rule is None:
        return Route(UNOWNED, UNOWNED, dataset)
    return Route(rule.owner, rule.team, dataset)


def naive_route(root: str, nodes: Mapping[str, Node], registry: Registry) -> Route:
    return _route_to(dataset_of(root, nodes), registry)


def route(root: str, nodes: Mapping[str, Node], registry: Registry) -> Route:
    dataset = dataset_of(root, nodes)
    node = nodes.get(dataset)
    if node is not None and node.resource_type == "model" and node.name.startswith(STAGING_PREFIX):
        sources = [p for p in node.depends_on if p.startswith("source.")]
        if len(sources) == 1:
            return _route_to(sources[0], registry)
    return _route_to(dataset, registry)
