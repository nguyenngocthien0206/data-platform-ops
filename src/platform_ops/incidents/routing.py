"""Who gets paged for an incident.

The page goes to the owner of the incident's root, with two exceptions, both
for data that arrived broken, which the owner of the feed can fix and the
owner of the model cannot:

- **Through column lineage**, at any layer: when every failing check on a
  model root is a test on columns traced entirely to one source
  (``metadata.column_lineage``, including the columns its joins, filters and
  groupings read), the data the test caught came from that source, and the
  page goes to its owner. A model that failed to build, a test on the whole
  table, or a column built from two sources keeps the model's owner.
- **A staging model that reads a single source**, when there is no lineage to
  go by. Staging models only rename and cast (a Phase 1 convention), so a
  staging failure almost always means the data arrived broken.

``naive_route`` is the rule without the exception. The report grades both, so
the value of the exception is measured rather than claimed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from platform_ops.incidents.ingest import CheckEvent
from platform_ops.incidents.severity import dataset_of
from platform_ops.metadata.column_lineage import ColumnLineage, LineageUnavailable
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
    # Who that rule names to page instead when the owner is busy.
    backup: str | None = None


def _route_to(dataset: str, registry: Registry) -> Route:
    rule = registry.resolve(dataset).rule
    if rule is None:
        return Route(UNOWNED, UNOWNED, dataset)
    return Route(rule.owner, rule.team, dataset, rule.backup)


def paged(page: Route, open_by_person: Mapping[str, int], max_open_per_person: int) -> str:
    """Who is paged: the owner, or the rule's backup when the owner is already busy."""
    if page.backup and open_by_person.get(page.owner, 0) >= max_open_per_person:
        return page.backup
    return page.owner


def naive_route(root: str, nodes: Mapping[str, Node], registry: Registry) -> Route:
    return _route_to(dataset_of(root, nodes), registry)


def route(
    root: str,
    nodes: Mapping[str, Node],
    registry: Registry,
    events: Sequence[CheckEvent] = (),
    lineage: ColumnLineage | None = None,
) -> Route:
    """Who is paged for ``root``, given the checks that failed on it (``events``)."""
    dataset = dataset_of(root, nodes)
    source = source_of_failing_columns(dataset, nodes, events, lineage)
    if source is not None:
        return _route_to(source, registry)
    node = nodes.get(dataset)
    if node is not None and node.resource_type == "model" and node.name.startswith(STAGING_PREFIX):
        sources = [p for p in node.depends_on if p.startswith("source.")]
        if len(sources) == 1:
            return _route_to(sources[0], registry)
    return _route_to(dataset, registry)


def source_of_failing_columns(
    dataset: str,
    nodes: Mapping[str, Node],
    events: Sequence[CheckEvent],
    lineage: ColumnLineage | None,
) -> str | None:
    """The one source every failing column on ``dataset`` is traced to, if there is one."""
    node = nodes.get(dataset)
    own = [e for e in events if e.subject == dataset]
    if lineage is None or node is None or node.resource_type != "model" or not own:
        return None
    columns: set[str] = set()
    for event in own:
        test = nodes.get(event.check_id)
        if event.check_type != "test" or test is None or not test.test_columns:
            return None
        columns |= set(test.test_columns)
    try:
        traced = lineage.model(dataset)
    except LineageUnavailable:
        return None
    sources: set[str] = set()
    for column in columns:
        reads = traced.get(column)
        if not reads:
            return None
        sources |= {f"{lineage.raw_source}{table}" for table, _ in reads}
    return sources.pop() if len(sources) == 1 else None
