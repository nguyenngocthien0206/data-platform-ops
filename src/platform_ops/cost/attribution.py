"""Who pays for each query.

Every query's cost is charged exactly once, so team totals always add up to the
bill (ADR 0006):

- **Production cost**: a dbt build is charged to the owner of the model it
  writes. A dbt test is charged to the owner of the model it tests, because
  testing is part of what it costs to publish that model.
- **Consumption cost**: a dashboard refresh is charged to the dashboard owner's
  team; an ad hoc query to the team of the person who ran it (from
  ``config/teams.yaml``).

Charging the reader of a table, not its owner, is what makes a team see the cost
of its own dashboards and habits, instead of the platform team quietly
absorbing everyone's ``SELECT *``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import duckdb

from platform_ops.common.db import OPS_SCHEMA, insert_rows, transaction
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import Registry, Resolution

UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class Attribution:
    query_id: str
    cost_kind: str  # production | consumption
    team: str
    owner: str
    subject: str | None  # the model a production cost belongs to, or the dashboard


def attribute(
    rows: list[tuple[str, str, str, str | None]],
    nodes: Mapping[str, Node],
    resolutions: Mapping[str, Resolution],
    registry: Registry,
) -> list[Attribution]:
    """``rows`` are ``(query_id, actor, actor_type, node_id)`` from ``ops.query_log``."""

    def owner_of(unique_id: str | None) -> tuple[str, str]:
        resolution = resolutions.get(unique_id or "")
        if resolution is None or resolution.rule is None:
            return UNATTRIBUTED, UNATTRIBUTED
        return resolution.rule.team, resolution.rule.owner

    out: list[Attribution] = []
    for query_id, actor, actor_type, node_id in rows:
        if actor_type == "dbt":
            node = nodes.get(node_id or "")
            subject = node.attached_node if node and node.resource_type == "test" else node_id
            team, owner = owner_of(subject)
            out.append(Attribution(query_id, "production", team, owner, subject))
        elif actor_type == "dashboard":
            team, owner = owner_of(node_id)
            out.append(Attribution(query_id, "consumption", team, owner, node_id))
        else:
            team = registry.team_of(actor) or UNATTRIBUTED
            out.append(Attribution(query_id, "consumption", team, actor, None))
    return out


def persist(connection: duckdb.DuckDBPyConnection, attributions: list[Attribution]) -> int:
    connection.execute(
        f"""CREATE OR REPLACE TABLE {OPS_SCHEMA}.query_attribution (
                query_id VARCHAR PRIMARY KEY,
                cost_kind VARCHAR NOT NULL,
                team VARCHAR NOT NULL,
                owner VARCHAR NOT NULL,
                subject VARCHAR
            )"""
    )
    rows = [(a.query_id, a.cost_kind, a.team, a.owner, a.subject) for a in attributions]
    with transaction(connection):
        insert_rows(
            connection,
            f"{OPS_SCHEMA}.query_attribution",
            ("query_id", "cost_kind", "team", "owner", "subject"),
            rows,
        )
    return len(rows)
