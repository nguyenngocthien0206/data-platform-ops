"""Who pays for each query, and for each table's storage.

Every query's cost is charged exactly once, so team totals always add up to the
bill (ADR 0004):

- **Production cost** goes to the owner of what was built or loaded: a dbt
  build to the owner of the model it writes, a dbt test to the owner of the
  model it tests, the ingestion and the nightly merge of a raw table to the
  owner of that source. dbt's own bookkeeping, outside any model, goes to the
  platform team, which runs dbt.
- **Consumption cost** goes to the reader: ad hoc SQL to the team of the
  person who typed it, and a dashboard refresh to the teams who opened that
  dashboard that month, shared by their views (``viewer_weights``, ``split``).
  The ``metabase`` user runs the refresh for whoever reads it; a dashboard
  nobody opened that month is paid by its owner, who keeps it refreshing.
- **Migration cost**: the parallel run's reconciliation, carried by the
  platform team, which runs the migration, and reported on its own.

Storage goes to the owner of each table. Charging the reader, not the owner,
is what makes a team see the cost of its own dashboards and habits, instead of
the platform team quietly absorbing everyone's ``SELECT *``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

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


# The ops store keeps costs at 12 decimals; a split is cut there, so its parts
# add up to the query's cost exactly once stored.
QUANTUM = Decimal("1e-12")


def viewer_weights(
    records: Iterable[QueryRecord],
    views: Iterable[tuple[datetime, str, str]],
    registry: Registry,
) -> dict[str, tuple[tuple[str, int], ...]]:
    """Each dashboard refresh's viewers: the teams that opened it that month, with their views.

    ``views`` are (viewed_at, exposure, person), as a BI tool's view log
    records them; a person outside the registry counts for nobody. A refresh
    of a dashboard nobody opened that month has no entry, and stays with the
    dashboard's owner.
    """
    counts: dict[tuple[str, str], Counter[str]] = {}
    for viewed_at, exposure, person in views:
        team = registry.team_of(person)
        if team is not None:
            counts.setdefault((exposure, f"{viewed_at:%Y-%m}"), Counter())[team] += 1
    found: dict[str, tuple[tuple[str, int], ...]] = {}
    for r in records:
        if r.actor_type != "dashboard":
            continue
        seen = counts.get((r.node_id or "", f"{r.started_at:%Y-%m}"))
        if seen:
            found[r.query_id] = tuple(sorted(seen.items()))
    return found


def split(usd: Decimal, weights: tuple[tuple[str, int], ...]) -> dict[str, Decimal]:
    """``usd`` shared by ``weights``, exactly.

    Each share is cut down to 12 decimals, and what is left goes to the team
    with the most views (the first by name on a tie), so the parts add up to
    ``usd`` to the last digit.
    """
    total = sum(n for _, n in weights)
    parts = {team: (usd * n / total).quantize(QUANTUM, ROUND_DOWN) for team, n in weights}
    largest = min(weights, key=lambda w: (-w[1], w[0]))[0]
    parts[largest] += usd - sum(parts.values())
    return parts
