"""What a query touched, worked out from its SQL with sqlglot.

The cost module prices a query by the columns it reads, charges it to the table
it writes, and looks for tables that are filtered on the same column again and
again. All three come from here. Resolution is scope-based: a column is traced
through CTEs and subqueries to the base table it really comes from, so a CTE
named ``orders`` is never mistaken for a table, and ``SELECT *`` expands to the
real columns of the table it reads.

Limitation, deliberately accepted: a filter written against a CTE or subquery
column (``WHERE cte.col > 1`` outside the CTE) is not traced back to its base
table, so it does not count towards hotspot detection. Filters written where
the table is read, which is how the workload and dbt write them, are traced.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

# "schema.table" -> {column: type}. Built from the warehouse catalog.
Catalog = Mapping[str, Mapping[str, str]]

DBT_TMP_SUFFIX = "__dbt_tmp"
_COMMENT = re.compile(r"/\*\s*(\{.*?\})\s*\*/", re.DOTALL)


@dataclass(frozen=True)
class TableUse:
    table: str
    columns: frozenset[str] = frozenset()
    filter_columns: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ParsedQuery:
    reads: dict[str, TableUse] = field(default_factory=dict)
    writes: str | None = None
    node_id: str | None = None
    select_star: bool = False
    columns_resolved: bool = True
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def table_key(table: exp.Table) -> str:
    """``schema.table`` in lower case, with dbt's temporary suffix removed.

    dbt-duckdb builds a model as ``<model>__dbt_tmp`` and renames it, so the
    statement it executes writes the temporary name. The cost belongs to the
    model, so the suffix is stripped here, once.
    """
    name = table.name.lower()
    if name.endswith(DBT_TMP_SUFFIX):
        name = name[: -len(DBT_TMP_SUFFIX)]
    schema = table.db.lower()
    return f"{schema}.{name}" if schema else name


def node_id_from_comment(sql: str) -> str | None:
    """The dbt node id from the JSON comment dbt appends to every query."""
    for match in _COMMENT.finditer(sql):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("unique_id"), str):
            return str(payload["unique_id"])
    return None


def _sqlglot_schema(catalog: Catalog) -> dict[str, object]:
    nested: dict[str, dict[str, dict[str, str]]] = {}
    for key, columns in catalog.items():
        schema, _, table = key.partition(".")
        nested.setdefault(schema, {})[table] = {c.lower(): t for c, t in columns.items()}
    return dict(nested.items())


def _drop_catalogs(tree: exp.Expr) -> None:
    """Queries may say ``"warehouse"."marts"."x"`` or ``marts.x``; treat them the same."""
    for table in tree.find_all(exp.Table):
        table.set("catalog", None)


def _write_target(tree: exp.Expr) -> tuple[str | None, exp.Expr | None]:
    """The table a statement writes, and the query that produces its rows."""
    if isinstance(tree, exp.Create):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        destination = table_key(target) if isinstance(target, exp.Table) else None
        return destination, tree.expression
    if isinstance(tree, exp.Insert):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        destination = table_key(target) if isinstance(target, exp.Table) else None
        return destination, tree.expression
    return None, tree


def _collect(scopes: list[Scope], uses: dict[str, dict[str, set[str]]]) -> None:
    for scope in scopes:
        # Every table read is registered, even when no column is, as in count(*).
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                uses.setdefault(table_key(source), {"columns": set(), "filters": set()})
        where = scope.expression.args.get("where") if scope.expression else None
        filtered = {id(c) for c in where.find_all(exp.Column)} if where is not None else set()
        for column in scope.columns:
            origin = scope.sources.get(column.table)
            if not isinstance(origin, exp.Table):
                continue
            entry = uses[table_key(origin)]
            entry["columns"].add(column.name.lower())
            if id(column) in filtered:
                entry["filters"].add(column.name.lower())


def parse_query(sql: str, catalog: Catalog, dialect: str = "duckdb") -> ParsedQuery:
    """Parse one statement and report what it read and wrote."""
    node_id = node_id_from_comment(sql)
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except SqlglotError as error:
        return ParsedQuery(node_id=node_id, error=f"parse: {error}".splitlines()[0])
    if len(statements) != 1:
        return ParsedQuery(node_id=node_id, error=f"expected 1 statement, got {len(statements)}")

    tree = statements[0]
    _drop_catalogs(tree)
    writes, body = _write_target(tree)
    if body is None:
        return ParsedQuery(writes=writes, node_id=node_id)
    select_star = any(
        isinstance(p, exp.Star) for s in body.find_all(exp.Select) for p in s.expressions
    )

    resolved = True
    try:
        qualified = qualify(
            body.copy(),
            schema=_sqlglot_schema(catalog),
            dialect=dialect,
            validate_qualify_columns=False,
        )
        scopes = traverse_scope(qualified)
    except SqlglotError:
        # Fall back to the tables alone; the estimate then bills their full width.
        resolved = False
        scopes = traverse_scope(body)

    uses: dict[str, dict[str, set[str]]] = {}
    _collect(scopes, uses)
    if writes is not None:
        uses.pop(writes, None)

    reads = {
        table: TableUse(table, frozenset(u["columns"]), frozenset(u["filters"]))
        for table, u in sorted(uses.items())
    }
    return ParsedQuery(
        reads=reads,
        writes=writes,
        node_id=node_id,
        select_star=select_star,
        columns_resolved=resolved,
    )
