"""Who pays for each query, and for each table's storage.

Every query's cost is charged exactly once, so team totals always add up to the
bill (ADR 0006):

- **Production cost** goes to the owner of what was built or loaded: a dbt
  build to the owner of the model it writes, a dbt test to the owner of the
  model it tests, the ingestion and the nightly merge of a raw table to the
  owner of that source. dbt's own bookkeeping, outside any model, goes to the
  platform team, which runs dbt.
- **Consumption cost** goes to the reader: a dashboard refresh to the owner of
  the dashboard (the ``metabase`` user only runs it for them), ad hoc SQL to
  the team of the person who typed it.
- **Migration cost**: the parallel run's reconciliation, carried by the
  platform team, which runs the migration, and reported on its own.

Storage goes to the owner of each table. Charging the reader, not the owner,
is what makes a team see the cost of its own dashboards and habits, instead of
the platform team quietly absorbing everyone's ``SELECT *``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from platform_ops.cost.collect import QueryRecord
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import Registry

UNATTRIBUTED = "unattributed"
PLATFORM = "platform"


@dataclass(frozen=True)
class Attribution:
    query_id: str
    cost_kind: str  # production | consumption | migration
    team: str
    owner: str
    subject: str | None  # the model, source or dashboard the cost belongs to


@dataclass(frozen=True)
class Owners:
    nodes: Mapping[str, Node]
    registry: Registry

    def of(self, unique_id: str | None) -> tuple[str, str]:
        if not unique_id:
            return UNATTRIBUTED, UNATTRIBUTED
        rule = self.registry.resolve(unique_id).rule
        if rule is None:
            return UNATTRIBUTED, UNATTRIBUTED
        return rule.team, rule.owner

    def subject(self, node_id: str | None) -> str | None:
        """What a dbt statement's cost belongs to: the model a test checks, else the node."""
        node = self.nodes.get(node_id or "")
        if node is not None and node.resource_type == "test":
            return node.attached_node
        return node_id

    def platform_lead(self) -> str:
        team = self.registry.teams.get(PLATFORM)
        return team.members[0] if team and team.members else UNATTRIBUTED

    def by_relation(self) -> dict[str, str]:
        """``database.table`` to the unique id of the node stored there."""
        return {
            n.relation: n.unique_id
            for n in self.nodes.values()
            if n.resource_type in ("model", "seed", "snapshot", "source")
        }


def attribute(records: Iterable[QueryRecord], owners: Owners) -> dict[str, Attribution]:
    out: dict[str, Attribution] = {}
    for r in records:
        if r.actor_type == "dbt":
            subject = owners.subject(r.node_id)
            if subject is None:
                out[r.query_id] = Attribution(
                    r.query_id, "production", PLATFORM, owners.platform_lead(), None
                )
                continue
            team, owner = owners.of(subject)
            out[r.query_id] = Attribution(r.query_id, "production", team, owner, subject)
        elif r.actor_type in ("ingestion", "maintenance"):
            team, owner = owners.of(r.node_id)
            out[r.query_id] = Attribution(r.query_id, "production", team, owner, r.node_id)
        elif r.actor_type == "reconcile":
            out[r.query_id] = Attribution(
                r.query_id, "migration", PLATFORM, owners.platform_lead(), r.node_id
            )
        elif r.actor_type == "dashboard":
            team, owner = owners.of(r.node_id)
            out[r.query_id] = Attribution(r.query_id, "consumption", team, owner, r.node_id)
        else:
            team = owners.registry.team_of(r.actor) or UNATTRIBUTED
            out[r.query_id] = Attribution(r.query_id, "consumption", team, r.actor, None)
    return out
