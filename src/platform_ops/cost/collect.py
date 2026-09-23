"""Collection: what every query did, from whichever system ran it.

Collection is kept apart from pricing. A collector only produces
:class:`QueryRecord` rows; pricing and attribution never care where a record
came from. The two local collectors here are the :class:`LoggedConnection`
wrapper, for dashboards and ad hoc users, and :func:`dbt_run_records`, which
reads a dbt run. A BigQuery or Snowflake collector (Phase 5) implements the
same :class:`QueryCollector` protocol against the vendor's query history, and
nothing downstream changes.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import duckdb
import networkx as nx

from platform_ops.common.clock import Clock
from platform_ops.common.config import ActorType
from platform_ops.common.db import OPS_SCHEMA, insert_rows, transaction
from platform_ops.cost.sql_parse import Catalog, ParsedQuery, parse_query
from platform_ops.metadata.manifest import Node

QUERY_LOG_COLUMNS = (
    "query_id",
    "run_id",
    "actor",
    "actor_type",
    "node_id",
    "started_at",
    "sql_text",
    "replayed",
    "wallclock_ms",
    "rows_scanned",
    "writes",
    "select_star",
    "columns_resolved",
    "parse_error",
)
QUERY_TABLES_COLUMNS = ("query_id", "table_name", "columns", "filter_columns")

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class QueryRecord:
    """One query as collected. Timestamps are simulated time."""

    query_id: str
    run_id: str
    actor: str
    actor_type: ActorType
    node_id: str | None
    started_at: datetime
    sql_text: str
    replayed: bool = False
    wallclock_ms: float | None = None
    rows_scanned: int | None = None


class QueryCollector(Protocol):
    """Anything that can produce query records: a local log or a vendor's history."""

    def records(self) -> Iterable[QueryRecord]:  # pragma: no cover - protocol
        ...


def catalog_from(connection: duckdb.DuckDBPyConnection) -> dict[str, dict[str, str]]:
    """``schema.table -> {column: type}`` for every table the workload can touch."""
    catalog: dict[str, dict[str, str]] = {}
    rows = connection.execute(
        """SELECT schema_name, table_name, column_name, data_type FROM duckdb_columns()
           WHERE schema_name IN ('raw', 'staging', 'intermediate', 'marts')
           ORDER BY schema_name, table_name, column_index"""
    ).fetchall()
    for schema, table, column, data_type in rows:
        catalog.setdefault(f"{schema}.{table}", {})[str(column)] = str(data_type)
    return catalog


def _shape(sql: str) -> str:
    """The SQL with literals blanked, so queries differing only in values share a parse."""
    return _NUMBER_LITERAL.sub("0", _STRING_LITERAL.sub("''", sql))


class QueryLog:
    """Buffers query records, parses them, and writes them to ``ops.query_log``."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._cache: dict[str, ParsedQuery] = {}
        self._log: list[tuple[Any, ...]] = []
        self._tables: list[tuple[Any, ...]] = []
        self.count = 0

    def set_catalog(self, catalog: Catalog) -> None:
        """Use a new catalog. Parses are kept when nothing changed, which is the
        normal case between weekly builds: re-parsing the same 274 dbt statements
        every week would cost minutes."""
        if catalog != self._catalog:
            self._catalog = catalog
            self._cache.clear()

    def parse(self, sql: str) -> ParsedQuery:
        key = _shape(sql)
        parsed = self._cache.get(key)
        if parsed is None:
            parsed = parse_query(sql, self._catalog)
            self._cache[key] = parsed
        return parsed

    def add(self, record: QueryRecord) -> None:
        parsed = self.parse(record.sql_text)
        self._log.append(
            (
                record.query_id,
                record.run_id,
                record.actor,
                record.actor_type,
                record.node_id,
                record.started_at,
                record.sql_text,
                record.replayed,
                record.wallclock_ms,
                record.rows_scanned,
                parsed.writes,
                parsed.select_star,
                parsed.columns_resolved,
                parsed.error,
            )
        )
        for use in parsed.reads.values():
            self._tables.append(
                (record.query_id, use.table, sorted(use.columns), sorted(use.filter_columns))
            )
        self.count += 1

    def flush(self, connection: duckdb.DuckDBPyConnection) -> None:
        if not self._log:
            return
        with transaction(connection):
            insert_rows(connection, f"{OPS_SCHEMA}.query_log", QUERY_LOG_COLUMNS, self._log)
            insert_rows(
                connection, f"{OPS_SCHEMA}.query_tables", QUERY_TABLES_COLUMNS, self._tables
            )
        self._log.clear()
        self._tables.clear()


class LoggedConnection:
    """A DuckDB connection that records every query it runs.

    Records the SQL as written, who ran it, the simulated start time, the real
    wall-clock duration and, from DuckDB's profiler, the rows actually scanned.
    Results are fetched one page at a time, like a SQL editor: a ``SELECT *``
    on a large table returns its first page without pulling every row into
    Python. When the page cap cuts a result short, DuckDB has not finished the
    scan, so rows scanned is recorded as unknown rather than as a wrong number.
    """

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        log: QueryLog,
        clock: Clock,
        page_rows: int,
    ) -> None:
        self._connection = connection
        self._log = log
        self._clock = clock
        self._page_rows = page_rows
        connection.execute("PRAGMA enable_profiling='no_output'")
        connection.execute("SET profiling_mode='standard'")

    def execute(
        self,
        sql: str,
        *,
        query_id: str,
        run_id: str,
        actor: str,
        actor_type: ActorType,
        node_id: str | None = None,
    ) -> list[tuple[Any, ...]]:
        started_at = self._clock.now()
        began = time.perf_counter()
        rows = self._connection.execute(sql).fetchmany(self._page_rows)
        wallclock_ms = (time.perf_counter() - began) * 1000
        rows_scanned: int | None = None
        if len(rows) < self._page_rows:
            profile = json.loads(self._connection.get_profiling_information(format="json"))
            rows_scanned = int(profile.get("cumulative_rows_scanned", 0))
        self._log.add(
            QueryRecord(
                query_id=query_id,
                run_id=run_id,
                actor=actor,
                actor_type=actor_type,
                node_id=node_id,
                started_at=started_at,
                sql_text=sql,
                wallclock_ms=round(wallclock_ms, 3),
                rows_scanned=rows_scanned,
            )
        )
        return rows


@dataclass(frozen=True)
class DbtRun:
    """The nodes one real dbt build executed, and the SQL it ran for each."""

    nodes: tuple[Node, ...]
    sql: Mapping[str, str]


def read_dbt_run(target_path: Path, nodes: Mapping[str, Node]) -> DbtRun:
    """Read ``run_results.json`` and the executed SQL in ``target/run``.

    Nodes are put in dependency order with ties broken by unique id, so the
    record order, and with it every modelled start time, is the same on every
    run whatever order dbt's threads happened to finish in.
    """
    results = json.loads((target_path / "run_results.json").read_text(encoding="utf-8"))
    # Exposures appear in a build's results as no-ops; only models and tests run SQL.
    executed = {
        r["unique_id"]
        for r in results["results"]
        if r["status"] not in ("skipped", "no-op")
        and r["unique_id"] in nodes
        and nodes[r["unique_id"]].resource_type in ("model", "test")
    }
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(sorted(executed))
    for unique_id in executed:
        for parent in nodes[unique_id].depends_on:
            if parent in executed:
                graph.add_edge(parent, unique_id)
    ordered = tuple(nodes[n] for n in nx.lexicographical_topological_sort(graph))

    sql: dict[str, str] = {}
    for node in ordered:
        path = node.run_file(target_path)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"no executed SQL for {node.unique_id} at {path}")
        sql[node.unique_id] = path.read_text(encoding="utf-8")
    return DbtRun(nodes=ordered, sql=sql)


def dbt_run_records(
    run: DbtRun, run_id: str, started_at: datetime, *, replayed: bool
) -> list[QueryRecord]:
    """One record per executed model and test, stamped with the scheduled run time.

    Every record gets the run's start time; the pricing queue then lays them out
    one after another on the transform warehouse, in this order.
    """
    return [
        QueryRecord(
            query_id=f"{run_id}:{index:04d}",
            run_id=run_id,
            actor="dbt",
            actor_type="dbt",
            node_id=node.unique_id,
            started_at=started_at,
            sql_text=run.sql[node.unique_id],
            replayed=replayed,
        )
        for index, node in enumerate(run.nodes)
    ]
