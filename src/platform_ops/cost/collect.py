"""Collection: what every query did, from whichever system ran it (version 2.0).

Collection is kept apart from pricing. A collector only produces
:class:`QueryRecord` rows; pricing and attribution never care where a record
came from (ADR 0006). The company's own collector reads ClickHouse's query log
(:func:`clickhouse_records`): every query its run tagged (``common.tagging``),
with the rows and bytes ClickHouse measured. The BigQuery and Snowflake
collectors in :mod:`platform_ops.cost.collectors` implement the same
:class:`QueryCollector` protocol against a vendor's query history.

Query text is sensitive: a literal can be a customer's email. What attribution
and the recommendations need is kept, the statement with every literal replaced
by ``?``; the text as run is parsed in memory and never stored or reported.

Two kinds of query are recorded but not billed: statements that only change
the catalogue (``CREATE``, ``DROP``, ``RENAME``...), and reads of ClickHouse's
own ``system`` tables, which dbt makes to plan its work. The second kind also
reads more or less depending on whatever else lives on the server, which no
company's bill should depend on; a cloud warehouse does not charge for either.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from psycopg import sql

from platform_ops.common.config import ActorType, Settings
from platform_ops.common.servers import clickhouse_client
from platform_ops.common.store import open_store, ops_table, replace_ops_table
from platform_ops.common.tagging import parse as parse_tag
from platform_ops.cost.sql_parse import Catalog, ParsedQuery, node_id_from_comment, parse_query

_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_NUMBER_LITERAL = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_MARKER = ""
# dbt-clickhouse names an incremental model's staging table after the run's
# invocation id (``<model>__dbt_new_data_<uuid>``), which is new every run.
_INVOCATION_ID = re.compile(r"_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}")
_MARKER_PATTERN = re.compile(_MARKER)

BILLED_KINDS = frozenset({"Select", "Insert"})
# Who holds the connection for each kind of work.
ACTOR_USERS = {
    "dbt": "dbt",
    "ingestion": "loader",
    "reconcile": "platform",
    "maintenance": "platform",
}


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
    # Measured by whoever ran the query: ClickHouse's read_bytes, or a vendor's
    # bytes billed when a cloud collector supplies it.
    bytes_scanned: int | None = None
    # Rows written repeat exactly between runs; bytes written do not (how
    # ClickHouse cuts blocks varies), so they are not collected.
    written_rows: int | None = None
    query_kind: str = ""
    # Tables the engine reports reading, as ``database.table``.
    tables: tuple[str, ...] = ()
    billable: bool = True


class QueryCollector(Protocol):
    """Anything that can produce query records: a query log or a vendor's history."""

    def records(self) -> Iterable[QueryRecord]:  # pragma: no cover - protocol
        ...


def redact(text: str) -> str:
    """The statement with its literals replaced by ``?`` (comments are kept)."""
    # Comments are set aside behind a marker with no digit or quote in it, so
    # the literal patterns cannot touch it, and put back in order.
    comments = iter(_COMMENT.findall(text))
    body = _COMMENT.sub(_MARKER, text)
    body = _NUMBER_LITERAL.sub("?", _STRING_LITERAL.sub("?", body))
    return _MARKER_PATTERN.sub(lambda _: next(comments), body)


@dataclass
class QueryLog:
    """Buffers query records and what their SQL read, for ``ops.query_log``."""

    catalog: Catalog = field(default_factory=dict)
    dialect: str = "clickhouse"
    records: list[QueryRecord] = field(default_factory=list)
    filters: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    # Statements left out because no warehouse bills them (see clickhouse_records).
    unbilled: int = 0
    _cache: dict[str, ParsedQuery] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.records)

    def parse(self, text: str) -> ParsedQuery:
        key = redact(text)
        parsed = self._cache.get(key)
        if parsed is None:
            parsed = parse_query(text, self.catalog, dialect=self.dialect)
            self._cache[key] = parsed
        return parsed

    def add(self, record: QueryRecord, *, raw_sql: str | None = None) -> None:
        """Keep the record, its text redacted. ``raw_sql`` (consumption queries
        only) is parsed for the columns its filters use, then dropped."""
        if raw_sql is not None:
            parsed = self.parse(raw_sql)
            for use in parsed.reads.values():
                if use.filter_columns:
                    self.filters.append(
                        (record.query_id, use.table, tuple(sorted(use.filter_columns)))
                    )
        self.records.append(QueryRecord(**{**record.__dict__, "sql_text": redact(record.sql_text)}))


# -- ClickHouse's query log -------------------------------------------------------------


def clickhouse_catalog(client: Any, databases: list[str]) -> dict[str, dict[str, str]]:
    """``database.table -> {column: type}`` for the parser to resolve columns."""
    catalog: dict[str, dict[str, str]] = {}
    rows = client.query(
        "SELECT database, table, name, type FROM system.columns "
        "WHERE database IN %(dbs)s ORDER BY database, table, position",
        parameters={"dbs": databases},
    ).result_rows
    for database, table, column, data_type in rows:
        catalog.setdefault(f"{database}.{table}", {})[str(column)] = str(data_type)
    return catalog


def _actor(tag: dict[str, str]) -> str:
    person = tag.get("person", "")
    if person:
        return person
    return {"dashboard": "metabase"}.get(tag["actor"], ACTOR_USERS.get(tag["actor"], tag["actor"]))


def clickhouse_records(settings: Settings, run_id: str) -> QueryLog:
    """Every query the company's run tagged, read back from ClickHouse's query log.

    The log's own ids and times are random and wall-clock, so neither is kept:
    records are put in a stable order (simulated time, kind of work, subject,
    then the query itself and what it measured) and numbered in it, so two runs
    produce the same ids for the same work. Only billable statements are kept.
    """
    from platform_ops.simulation.company import company_databases

    client = clickhouse_client(settings)
    try:
        client.command("SYSTEM FLUSH LOGS")
        rows = client.query(
            "SELECT log_comment, query_kind, query, read_rows, read_bytes, written_rows, "
            "tables FROM system.query_log "
            "WHERE type = 'QueryFinish' AND JSONExtractString(log_comment, 'run') = %(run)s",
            parameters={"run": run_id},
        ).result_rows
        log = QueryLog(catalog=clickhouse_catalog(client, company_databases(settings)))
    finally:
        client.close()

    entries = []
    for comment, kind, text, read_rows, read_bytes, written_rows, tables in rows:
        tag = parse_tag(str(comment))
        if tag is None:
            continue
        text = _INVOCATION_ID.sub("", str(text))
        own = tuple(
            sorted(
                _INVOCATION_ID.sub("", str(t)) for t in tables if not str(t).startswith("system.")
            )
        )
        reads_only_system = bool(tables) and not own
        entries.append(
            (
                tag["sim"],
                tag["actor"],
                tag.get("subject", ""),
                tag.get("person", ""),
                str(kind),
                redact(text),
                int(read_bytes),
                int(read_rows),
                int(written_rows),
                own,
                reads_only_system,
                text,
                tag,
            )
        )
    entries.sort(key=lambda e: e[:10])
    counters: dict[tuple[str, str], int] = {}
    for (
        sim,
        actor_type,
        subject,
        _person,
        kind,
        redacted,
        rb,
        rr,
        wr,
        own,
        only_system,
        text,
        tag,
    ) in entries:
        # Catalogue statements and reads of system tables are not billed, and
        # dbt sends one or two more or fewer of them from run to run, so they
        # are counted, not kept: numbering them would shift every id after them.
        if kind not in BILLED_KINDS or only_system:
            log.unbilled += 1
            continue
        seq = counters.get((sim, actor_type), 0)
        counters[(sim, actor_type)] = seq + 1
        node_id = node_id_from_comment(text) if actor_type == "dbt" else (subject or None)
        record = QueryRecord(
            query_id=f"{sim}/{actor_type}/{seq:06d}",
            run_id=run_id,
            actor=_actor(tag),
            actor_type=actor_type,  # type: ignore[arg-type]
            node_id=node_id,
            started_at=datetime.fromisoformat(sim),
            sql_text=redacted,
            rows_scanned=rr,
            bytes_scanned=rb,
            written_rows=wr,
            query_kind=kind,
            tables=own,
        )
        consumer = actor_type in ("dashboard", "adhoc")
        log.add(record, raw_sql=text if consumer else None)
    return log


def maintenance_records(settings: Settings, run_id: str) -> list[QueryRecord]:
    """The nightly OPTIMIZE of raw, from what each merge rewrote (``ops.raw_maintenance``)."""
    with open_store(settings) as store:
        rows = store.execute(
            sql.SQL(
                "SELECT sim_at, table_name, rows_rewritten, bytes_rewritten FROM {} "
                "ORDER BY sim_at, table_name"
            ).format(ops_table(settings, "raw_maintenance"))
        ).fetchall()
    raw = settings.company.name(settings.company.raw_database)
    return [
        QueryRecord(
            query_id=f"{sim_at.isoformat(timespec='seconds')}/maintenance/{table}",
            run_id=run_id,
            actor="platform",
            actor_type="maintenance",
            node_id=f"source.company.raw.{table}",
            started_at=sim_at,
            sql_text=f"OPTIMIZE TABLE {raw}.{table} FINAL",
            rows_scanned=int(rows),
            bytes_scanned=int(size),
            written_rows=int(rows),
            query_kind="Optimize",
            tables=(f"{raw}.{table}",),
        )
        for sim_at, table, rows, size in rows
    ]


QUERY_LOG_DDL = """
    query_id text PRIMARY KEY, actor text NOT NULL, actor_type text NOT NULL, node_id text,
    started_at timestamp NOT NULL, query_kind text NOT NULL, sql_text text NOT NULL,
    rows_scanned bigint, bytes_scanned bigint, written_rows bigint,
    tables text NOT NULL, billable boolean NOT NULL"""
QUERY_LOG_COLUMNS = [
    "query_id",
    "actor",
    "actor_type",
    "node_id",
    "started_at",
    "query_kind",
    "sql_text",
    "rows_scanned",
    "bytes_scanned",
    "written_rows",
    "tables",
    "billable",
]
QUERY_FILTERS_DDL = "query_id text NOT NULL, table_name text NOT NULL, filter_columns text NOT NULL"


def store(settings: Settings, log: QueryLog) -> int:
    """Replace ``ops.query_log`` and ``ops.query_filters`` with the collected run."""
    rows = [
        (
            r.query_id,
            r.actor,
            r.actor_type,
            r.node_id,
            r.started_at,
            r.query_kind,
            r.sql_text,
            r.rows_scanned,
            r.bytes_scanned,
            r.written_rows,
            ",".join(r.tables),
            r.billable,
        )
        for r in sorted(log.records, key=lambda r: r.query_id)
    ]
    filters = [(q, t, ",".join(c)) for q, t, c in sorted(log.filters)]
    with open_store(settings) as connection:
        replace_ops_table(connection, settings, "query_log", QUERY_LOG_DDL, QUERY_LOG_COLUMNS, rows)
        replace_ops_table(
            connection,
            settings,
            "query_filters",
            QUERY_FILTERS_DDL,
            ["query_id", "table_name", "filter_columns"],
            filters,
        )
    return len(rows)
