"""What the bill says to change, from the measured workload (version 2.0, Phase 13).

- **Unused tables.** A model is unused when neither it nor anything downstream
  of it was read by a dashboard or a person in the lookback window. dbt reading
  a table to build another does not count, so a staging model that feeds a
  live dashboard is never flagged, and one that only feeds dead tables is.
- **Full-scan hotspots.** ClickHouse measures the rows each query read. A
  dashboard or ad hoc query that filters a large table but still reads nearly
  all of it (``hotspot_scan_share``) is not using the table's sort key; when
  the same table is filtered that way on the same column often enough, the
  report suggests putting that column first in the sort key, or, on a
  ``ReplacingMergeTree`` (whose sort key is what it deduplicates on), a
  skip index.
- **Incremental candidates.** The most expensive models still rebuilt in full,
  checked against their sources: a model built on a source whose rows change
  in place (observed in the legacy database, ``ops.source_changes``) cannot
  simply append, so it is ruled out.
- **Quotas.** For each person-facing user (the teams and Metabase), what it
  read on its busiest days, and a quota and a per-query limit a little above
  that, as statements the platform team can apply or not.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import networkx as nx

from platform_ops.cost.attribution import Owners
from platform_ops.cost.collect import QueryRecord
from platform_ops.metadata.lineage import downstream, upstream

CONSUMERS = ("dashboard", "adhoc")
HOTSPOT_SCAN_SHARE = 0.9


@dataclass(frozen=True)
class UnusedTable:
    lookback_days: int
    unique_id: str
    relation: str
    team: str
    owner: str
    monthly_saving: dict[str, Decimal]


def _monthly(total: Decimal, window_days: int) -> Decimal:
    return (total * Decimal(30) / Decimal(window_days)).quantize(Decimal("0.0001"))


def unused_tables(
    records: Sequence[QueryRecord],
    owners: Owners,
    graph: nx.DiGraph[str],
    production: Mapping[str, Mapping[str, Decimal]],
    window_end: datetime,
    window_days: int,
    lookbacks: Sequence[int],
) -> list[UnusedTable]:
    relations = owners.by_relation()
    written = {
        owners.subject(r.node_id)
        for r in records
        if r.actor_type == "dbt" and r.query_kind == "Insert" and r.node_id
    }
    models = sorted(
        uid
        for uid in written
        if uid and owners.nodes.get(uid) and owners.nodes[uid].resource_type == "model"
    )
    out: list[UnusedTable] = []
    for days in sorted(lookbacks):
        since = window_end - timedelta(days=days)
        read = {
            relations[t]
            for r in records
            if r.actor_type in CONSUMERS and r.started_at >= since
            for t in r.tables
            if t in relations
        }
        for unique_id in models:
            if unique_id in read or downstream(graph, unique_id) & read:
                continue
            team, owner = owners.of(unique_id)
            out.append(
                UnusedTable(
                    lookback_days=days,
                    unique_id=unique_id,
                    relation=owners.nodes[unique_id].relation,
                    team=team,
                    owner=owner,
                    monthly_saving={
                        model: _monthly(usd, window_days)
                        for model, usd in sorted(production.get(unique_id, {}).items())
                    },
                )
            )
    return out


@dataclass(frozen=True)
class Hotspot:
    table: str
    column: str
    reads: int
    table_rows: int
    rows_scanned: int
    bytes_scanned: int
    sorting_key: str
    engine: str = ""

    @property
    def suggestion(self) -> str:
        """Put the filtered column first in the sort key, unless the key is also
        what the engine deduplicates on: then a skip index is the lever."""
        if self.engine.startswith("Replacing"):
            return f"a minmax skip index on `{self.column}`: the sort key is the deduplication key"
        rest = [
            part.strip()
            for part in self.sorting_key.strip("()").split(",")
            if part.strip() and part.strip() != self.column
        ]
        return "sort key `(" + ", ".join([self.column, *rest]) + ")`"


def hotspots(
    records: Sequence[QueryRecord],
    filters: Iterable[tuple[str, str, tuple[str, ...]]],
    table_rows: Mapping[str, int],
    table_bytes: Mapping[str, int],
    sorting_keys: Mapping[str, str],
    min_table_bytes: int,
    min_reads: int,
    engines: Mapping[str, str] | None = None,
) -> list[Hotspot]:
    engines = engines or {}
    by_id = {r.query_id: r for r in records}
    found: dict[tuple[str, str], list[QueryRecord]] = defaultdict(list)
    for query_id, table, columns in filters:
        record = by_id.get(query_id)
        if record is None or record.actor_type not in CONSUMERS or record.tables != (table,):
            continue
        rows = table_rows.get(table, 0)
        if not rows or table_bytes.get(table, 0) < min_table_bytes:
            continue
        if (record.rows_scanned or 0) < HOTSPOT_SCAN_SHARE * rows:
            continue
        for column in columns:
            found[(table, column)].append(record)
    out = [
        Hotspot(
            table=table,
            column=column,
            reads=len(queries),
            table_rows=table_rows[table],
            rows_scanned=sum(q.rows_scanned or 0 for q in queries),
            bytes_scanned=sum(q.bytes_scanned or 0 for q in queries),
            sorting_key=sorting_keys.get(table, ""),
            engine=engines.get(table, ""),
        )
        for (table, column), queries in found.items()
        if len(queries) >= min_reads
    ]
    return sorted(out, key=lambda h: (-h.bytes_scanned, h.table, h.column))


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
    owners: Owners,
    graph: nx.DiGraph[str],
    production: Mapping[str, Mapping[str, Decimal]],
    materialized: Mapping[str, str],
    changed_in_place: set[str],
    rank_by: str,
    top_n: int,
    exclude: set[str],
) -> list[IncrementalCandidate]:
    """The ``top_n`` most expensive models still built in full, with a verdict."""
    nodes = owners.nodes
    ranked = sorted(
        (
            (costs.get(rank_by, Decimal(0)), uid)
            for uid, costs in production.items()
            if uid in nodes
            and nodes[uid].resource_type == "model"
            and materialized.get(uid) not in ("incremental", "view")
            and uid not in exclude
        ),
        key=lambda item: (-item[0], item[1]),
    )[:top_n]
    out = []
    for _, unique_id in ranked:
        sources = tuple(
            sorted(
                nodes[a].name
                for a in upstream(graph, unique_id)
                if nodes[a].resource_type == "source"
            )
        )
        team, _ = owners.of(unique_id)
        out.append(
            IncrementalCandidate(
                unique_id=unique_id,
                relation=nodes[unique_id].relation,
                team=team,
                production=dict(production[unique_id]),
                sources=sources,
                blocked_by=tuple(s for s in sources if s in changed_in_place),
            )
        )
    return out


@dataclass(frozen=True)
class QuotaSuggestion:
    user: str
    team: str
    active_days: int
    p95_daily_bytes: int
    max_query_bytes: int
    daily_quota_bytes: int
    query_limit_bytes: int
    statements: tuple[str, ...] = field(default=())


def _round_up(value: float) -> int:
    """Up to two significant figures, so a suggested limit reads like one a person set."""
    if value <= 0:
        return 0
    magnitude = 10 ** max(0, math.floor(math.log10(value)) - 1)
    return int(math.ceil(value / magnitude) * magnitude)


def _p95(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def quotas(
    records: Sequence[QueryRecord],
    team_of_user: Mapping[str, str],
    namespace: str,
    exclude: Iterable[str] = (),
    headroom: float = 2.0,
) -> list[QuotaSuggestion]:
    """A daily read quota at ``headroom`` times the 95th percentile day, and a
    per-query limit at ``headroom`` times the largest query, per person-facing user.

    Users in ``exclude`` also run the pipeline itself, so they get no suggestion."""
    skip = set(exclude)
    daily: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    largest: dict[str, int] = defaultdict(int)
    for r in records:
        if r.actor_type not in CONSUMERS or not r.billable:
            continue
        user = "metabase" if r.actor_type == "dashboard" else team_of_user.get(r.actor, "")
        if not user or user in skip:
            continue
        daily[user][f"{r.started_at:%Y-%m-%d}"] += r.bytes_scanned or 0
        largest[user] = max(largest[user], r.bytes_scanned or 0)
    out = []
    for user in sorted(daily):
        p95 = _p95(list(daily[user].values()))
        quota = _round_up(headroom * p95)
        limit = _round_up(headroom * largest[user])
        name = f"{namespace}{user}"
        out.append(
            QuotaSuggestion(
                user=user,
                team=user if user != "metabase" else "(all dashboards)",
                active_days=len(daily[user]),
                p95_daily_bytes=p95,
                max_query_bytes=largest[user],
                daily_quota_bytes=quota,
                query_limit_bytes=limit,
                statements=(
                    f"CREATE QUOTA {name}_daily FOR INTERVAL 1 day "
                    f"MAX read_bytes = {quota} TO {name}",
                    # ALTER USER ... SETTINGS replaces the user's settings, so the
                    # reader profile (simulation.access) is restated.
                    f"ALTER USER {name} SETTINGS PROFILE '{namespace}reader', "
                    f"max_bytes_to_read = {limit}",
                ),
            )
        )
    return out
