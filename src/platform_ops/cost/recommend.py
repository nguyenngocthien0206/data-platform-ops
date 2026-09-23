"""Where the money goes that nobody needed to spend.

- **Unused tables**: built in the window, but neither the table nor anything
  downstream of it was read by a dashboard or a person in the last N days.
  dbt's own reads do not count: a table only read by other dbt models is used
  exactly when those models are. That is how lineage keeps an intermediate
  table that feeds a live dashboard off the list.
- **Full-scan hotspots**: large tables that dashboards and people filter on the
  same column again and again, each time paying to scan the whole column.
  Partitioning or clustering on that column is the usual fix.
- **Incremental candidates**: the most expensive models to build, checked
  against their upstream sources. Rebuilding incrementally is only safe when
  every source only ever gains rows, which ``cost.growth`` observed day by day.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import duckdb
import networkx as nx

from platform_ops.common.db import OPS_SCHEMA
from platform_ops.metadata.lineage import downstream, upstream
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import Resolution

CONSUMER_ACTORS = ("dashboard", "adhoc")
PRICING_MODELS = ("scan", "compute")


def production_costs(connection: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Decimal]]:
    """Build and test cost per model over the window, per pricing model."""
    rows = connection.execute(
        f"""SELECT a.subject, c.model, sum(c.usd)
            FROM {OPS_SCHEMA}.query_costs c
            JOIN {OPS_SCHEMA}.query_attribution a USING (query_id)
            WHERE a.cost_kind = 'production' AND a.subject IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2"""
    ).fetchall()
    costs: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for subject, model, usd in rows:
        costs[str(subject)][str(model)] = Decimal(usd)
    return dict(costs)


def _monthly(total: Decimal, window_days: int) -> Decimal:
    return total * Decimal(30) / Decimal(window_days)


@dataclass(frozen=True)
class UnusedTable:
    lookback_days: int
    unique_id: str
    relation: str
    team: str
    owner: str
    monthly_saving: dict[str, Decimal]


def unused_tables(
    connection: duckdb.DuckDBPyConnection,
    nodes: Mapping[str, Node],
    graph: nx.DiGraph[str],
    resolutions: Mapping[str, Resolution],
    window_end: datetime,
    window_days: int,
    lookbacks: Sequence[int],
) -> list[UnusedTable]:
    by_relation = {n.relation: n.unique_id for n in nodes.values() if n.resource_type == "model"}
    written = {
        str(r[0])
        for r in connection.execute(
            f"""SELECT DISTINCT writes FROM {OPS_SCHEMA}.query_log
                WHERE actor_type = 'dbt' AND writes IS NOT NULL"""
        ).fetchall()
    }
    costs = production_costs(connection)
    out: list[UnusedTable] = []
    for days in sorted(lookbacks):
        since = window_end - timedelta(days=days)
        read = {
            str(r[0])
            for r in connection.execute(
                f"""SELECT DISTINCT qt.table_name
                    FROM {OPS_SCHEMA}.query_tables qt
                    JOIN {OPS_SCHEMA}.query_log ql USING (query_id)
                    WHERE ql.actor_type IN {CONSUMER_ACTORS} AND ql.started_at >= ?""",
                [since],
            ).fetchall()
        }
        read_ids = {by_relation[t] for t in read if t in by_relation}
        for relation in sorted(written):
            unique_id = by_relation.get(relation)
            if unique_id is None:
                continue
            used = unique_id in read_ids or bool(downstream(graph, unique_id) & read_ids)
            if used:
                continue
            resolution = resolutions.get(unique_id)
            rule = resolution.rule if resolution else None
            model_costs = costs.get(unique_id, {})
            out.append(
                UnusedTable(
                    lookback_days=days,
                    unique_id=unique_id,
                    relation=relation,
                    team=rule.team if rule else "unattributed",
                    owner=rule.owner if rule else "unattributed",
                    monthly_saving={
                        m: _monthly(model_costs.get(m, Decimal(0)), window_days)
                        for m in PRICING_MODELS
                    },
                )
            )
    return out


@dataclass(frozen=True)
class Hotspot:
    table: str
    column: str
    reads: int
    table_bytes: int
    bytes_scanned: int
    scan_usd: Decimal


def hotspots(
    connection: duckdb.DuckDBPyConnection, min_table_bytes: int, min_reads: int
) -> list[Hotspot]:
    rows = connection.execute(
        f"""
        WITH filtered AS (
            SELECT qt.query_id, qt.table_name, unnest(qt.filter_columns) AS column_name
            FROM {OPS_SCHEMA}.query_tables qt
            JOIN {OPS_SCHEMA}.query_log ql USING (query_id)
            WHERE ql.actor_type IN {CONSUMER_ACTORS}
        ),
        latest AS (
            SELECT table_name, sum(logical_bytes)::BIGINT AS bytes
            FROM {OPS_SCHEMA}.table_sizes s
            WHERE snapshot_at = (SELECT max(snapshot_at) FROM {OPS_SCHEMA}.table_sizes t
                                 WHERE t.table_name = s.table_name)
            GROUP BY table_name
        ),
        scan_cost AS (
            SELECT query_id, usd FROM {OPS_SCHEMA}.query_costs WHERE model = 'scan'
        )
        SELECT f.table_name, f.column_name, count(*) AS reads, l.bytes,
               sum(b.bytes)::BIGINT AS bytes_scanned,
               sum(sc.usd) AS scan_usd
        FROM filtered f
        JOIN latest l USING (table_name)
        JOIN {OPS_SCHEMA}.query_table_bytes b
          ON b.query_id = f.query_id AND b.table_name = f.table_name
        JOIN scan_cost sc ON sc.query_id = f.query_id
        GROUP BY f.table_name, f.column_name, l.bytes
        HAVING count(*) >= ? AND l.bytes >= ?
        ORDER BY bytes_scanned DESC, f.table_name, f.column_name
        """,
        [min_reads, min_table_bytes],
    ).fetchall()
    return [
        Hotspot(str(t), str(c), int(n), int(b), int(s), Decimal(u)) for t, c, n, b, s, u in rows
    ]


@dataclass(frozen=True)
class IncrementalCandidate:
    unique_id: str
    relation: str
    team: str
    production: dict[str, Decimal]
    sources: tuple[str, ...]
    blocked_by: tuple[str, ...]

    @property
    def candidate(self) -> bool:
        return not self.blocked_by


def incremental_candidates(
    connection: duckdb.DuckDBPyConnection,
    nodes: Mapping[str, Node],
    graph: nx.DiGraph[str],
    resolutions: Mapping[str, Resolution],
    append_only: Mapping[str, bool],
    top_n: int,
    exclude: set[str],
) -> list[IncrementalCandidate]:
    """The ``top_n`` most expensive live models under scan pricing, with a verdict."""
    costs = production_costs(connection)
    ranked = sorted(
        (
            (c.get("scan", Decimal(0)), uid)
            for uid, c in costs.items()
            if uid in nodes and nodes[uid].resource_type == "model" and uid not in exclude
        ),
        key=lambda item: (-item[0], item[1]),
    )[:top_n]
    out = []
    for _, unique_id in ranked:
        node = nodes[unique_id]
        sources = tuple(
            sorted(
                nodes[a].relation
                for a in upstream(graph, unique_id)
                if nodes[a].resource_type == "source"
            )
        )
        blocked = tuple(s for s in sources if not append_only.get(s, False))
        resolution = resolutions.get(unique_id)
        out.append(
            IncrementalCandidate(
                unique_id=unique_id,
                relation=node.relation,
                team=resolution.rule.team if resolution and resolution.rule else "unattributed",
                production=costs[unique_id],
                sources=sources,
                blocked_by=blocked,
            )
        )
    return out
