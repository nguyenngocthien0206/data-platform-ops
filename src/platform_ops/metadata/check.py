"""`platform-ops metadata check`: every dataset has exactly one owner.

Fails on an unowned model, source or exposure; on ambiguous ownership; on a
rule naming an unknown team or an owner who is not on that team; and on an
exposure whose dbt ``owner`` disagrees with the registry. Warns, without
failing, on rules that match nothing, so renaming a model stays a one-file
change and the stale rule shows up in the output until someone removes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from platform_ops.common.db import OPS_SCHEMA, transaction
from platform_ops.metadata.manifest import OWNED_TYPES, Node
from platform_ops.metadata.registry import Registry, Resolution


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    coverage: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors


def run_check(registry: Registry, nodes: dict[str, Node]) -> CheckReport:
    report = CheckReport()
    report.errors.extend(registry.problems())

    owned_nodes = sorted(
        (node for node in nodes.values() if node.resource_type in OWNED_TYPES),
        key=lambda node: node.unique_id,
    )
    matched_patterns: set[str] = set()

    for node in owned_nodes:
        resolution = registry.resolve(node.unique_id)
        report.resolutions[node.unique_id] = resolution
        matched_patterns.update(rule.match for rule in resolution.candidates)

        if resolution.rule is None:
            report.errors.append(f"{node.unique_id} has no owner")
        elif resolution.ambiguous:
            tied = ", ".join(f"'{rule.match}'" for rule in resolution.candidates[:2])
            report.errors.append(
                f"{node.unique_id} has ambiguous ownership: {tied} are equally specific"
            )
        elif node.resource_type == "exposure" and node.owner_name != resolution.rule.owner:
            report.errors.append(
                f"{node.unique_id} declares owner '{node.owner_name}' in dbt but the "
                f"registry says '{resolution.rule.owner}'"
            )

    for rule in registry.rules:
        if rule.match not in matched_patterns:
            report.warnings.append(f"rule '{rule.match}' matches no model, source or exposure")

    for resource_type in OWNED_TYPES:
        of_type = [n for n in owned_nodes if n.resource_type == resource_type]
        owned = sum(1 for n in of_type if report.resolutions[n.unique_id].owned)
        report.coverage[resource_type] = (owned, len(of_type))
    return report


def persist_ownership(
    connection: duckdb.DuckDBPyConnection, report: CheckReport, nodes: dict[str, Node]
) -> int:
    """Replace ``ops.node_ownership`` with the resolved owner of every dataset."""
    rows = [
        (
            unique_id,
            nodes[unique_id].resource_type,
            nodes[unique_id].name,
            resolution.rule.owner,
            resolution.rule.team,
            resolution.rule.tier,
            resolution.rule.match,
        )
        for unique_id, resolution in sorted(report.resolutions.items())
        if resolution.owned and resolution.rule is not None
    ]
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA}")
    with transaction(connection):
        connection.execute(
            f"""CREATE OR REPLACE TABLE {OPS_SCHEMA}.node_ownership (
                    unique_id VARCHAR PRIMARY KEY,
                    resource_type VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    owner VARCHAR NOT NULL,
                    team VARCHAR NOT NULL,
                    tier VARCHAR NOT NULL,
                    matched_pattern VARCHAR NOT NULL
                )"""
        )
        if rows:
            connection.executemany(
                f"INSERT INTO {OPS_SCHEMA}.node_ownership VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
    return len(rows)
