"""Column lineage: which raw columns each column of a dbt model is computed from.

dbt's manifest says which models a model reads, not which of their columns.
The reconciliation needs the finer answer: a report that differs is explained
by a copy defect only if the report reads the columns that differ. So each
model's compiled SQL (``target/compiled``) is parsed with sqlglot, its columns
qualified against the warehouse's own catalogue, and traced to the relations
it reads; models are followed down to the raw sources.

A column depends on the columns its expression is computed from, and on the
columns that decide which rows reach it. ``WHERE``, ``GROUP BY``, ``HAVING``,
an inner join's ``ON`` and ``DISTINCT`` decide the rows of the whole query, so
they count for every column of it: a filter on ``status`` changes a count. A
``LEFT JOIN`` keeps every row on its left, so its ``ON`` counts only for the
columns taken from the joined side: joining customers through a map built on
email changes a customer's country, not an order's amount. To let sqlglot
carry these through CTEs and subqueries, each is folded into the select
expressions of its own query before tracing (the SQL is traced, never run).
When in doubt the rule makes a column depend on more, never on less.

A model whose SQL sqlglot cannot parse or qualify raises ``LineageUnavailable``,
and the caller decides what to fall back to.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

DIALECT = "clickhouse"

# A raw column: (source table name, column name).
RawColumn = tuple[str, str]
# The warehouse catalogue: database -> table -> column -> type.
Catalogue = Mapping[str, Mapping[str, Mapping[str, str]]]


class LineageUnavailable(RuntimeError):
    """A model's SQL could not be traced column by column."""


@dataclass
class ColumnLineage:
    """Column lineage over one dbt project, traced lazily and remembered.

    ``manifest`` is dbt's parsed manifest, ``compiled`` the project's folder
    under ``target/compiled`` (``target/compiled/<project>``), ``catalogue``
    the warehouse's columns, and ``raw_source`` the prefix of the sources that
    count as raw (``source.company.raw.``).
    """

    manifest: Mapping[str, Any]
    compiled: Path
    catalogue: Catalogue
    raw_source: str = "source.company.raw."
    _relations: dict[tuple[str, str], str] = field(default_factory=dict, init=False)
    _schema: MappingSchema = field(init=False)
    _traced: dict[str, dict[str, frozenset[RawColumn]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._schema = MappingSchema(_plain(self.catalogue), dialect=DIALECT)
        for uid, node in self.manifest["nodes"].items():
            if node.get("resource_type") in ("model", "seed", "snapshot"):
                name = node.get("alias") or node["name"]
                self._relations[(node["schema"], name)] = uid
        for uid, source in self.manifest["sources"].items():
            self._relations[(source["schema"], source["identifier"])] = uid

    def model(self, uid: str) -> dict[str, frozenset[RawColumn]]:
        """Each column of model ``uid`` and the raw columns it depends on."""
        if uid not in self._traced:
            self._traced[uid] = self._trace(uid)
        return self._traced[uid]

    # -- one model ----------------------------------------------------------------------

    def _sql(self, uid: str) -> str:
        node = self.manifest["nodes"][uid]
        path: Path = self.compiled / str(node["original_file_path"])
        if not path.is_file():
            raise LineageUnavailable(f"{uid}: no compiled SQL at {path}")
        return path.read_text(encoding="utf-8")

    def _trace(self, uid: str) -> dict[str, frozenset[RawColumn]]:
        try:
            tree = qualify(
                sqlglot.parse_one(self._sql(uid), read=DIALECT),
                schema=self._schema,
                dialect=DIALECT,
            )
            if not isinstance(tree, exp.Query):
                raise LineageUnavailable(f"{uid}: not a query")
            _fold_row_shaping(tree)
            nodes = lineage(None, tree, schema=self._schema, dialect=DIALECT, copy=False)
            assert isinstance(nodes, dict)
            return {name: self._resolve(_leaves(node)) for name, node in nodes.items()}
        except LineageUnavailable:
            raise
        except Exception as error:  # sqlglot's own errors, whatever their class
            raise LineageUnavailable(f"{uid}: {type(error).__name__}: {error}") from error

    def _resolve(self, columns: set[tuple[str, str, str]]) -> frozenset[RawColumn]:
        """Relation columns (database, table, column) down to raw columns."""
        found: set[RawColumn] = set()
        for database, table, column in columns:
            uid = self._relations.get((database, table))
            if uid is None:
                continue  # not part of the project
            if uid.startswith(self.raw_source):
                found.add((uid.removeprefix(self.raw_source), column))
            elif uid.startswith("model."):
                upstream = self.model(uid)
                if column not in upstream:
                    raise LineageUnavailable(f"{uid}: no column {column} to trace")
                found |= upstream[column]
            # Seeds and snapshots are not compared, so they explain nothing.
        return frozenset(found)


def _plain(catalogue: Catalogue) -> dict[str, object]:
    return {db: {t: dict(cols) for t, cols in tables.items()} for db, tables in catalogue.items()}


def _table(source: exp.Table, column: str) -> tuple[str, str, str]:
    return (source.db, source.name, column)


def _leaves(node: Node) -> set[tuple[str, str, str]]:
    """The relation columns at the bottom of one column's lineage."""
    found = set()
    for item in node.walk():
        if not item.downstream and isinstance(item.expression, exp.Table):
            found.add(_table(item.expression, item.name.split(".")[-1].strip('"')))
    return found


def _fold_row_shaping(tree: exp.Query) -> None:
    """Fold, into each select expression, the columns that decide its rows.

    Each query keeps its own: ``WHERE``, ``GROUP BY``, ``HAVING``, ``QUALIFY``,
    the ``ON`` of an inner, right or full join, and every selected column under
    ``DISTINCT`` go to all its expressions; a ``LEFT JOIN``'s ``ON`` goes to the
    expressions that read the joined side. An expression ``e`` becomes
    ``tuple(e, c1, c2, ...)``, which changes its lineage and nothing else.
    """
    for scope in traverse_scope(tree):
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        every: list[exp.Column] = []
        for key in ("where", "group", "having", "qualify"):
            clause = select.args.get(key)
            if clause is not None:
                every += clause.find_all(exp.Column)
        joined: dict[str, list[exp.Column]] = {}
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            columns = list(on.find_all(exp.Column)) if on is not None else []
            if join.side == "LEFT":
                joined[join.this.alias_or_name] = columns
            else:
                every += columns
        if select.args.get("distinct") is not None:
            every += [c for e in select.selects for c in e.find_all(exp.Column)]
        for projection in select.selects:
            value = projection.this if isinstance(projection, exp.Alias) else projection
            extra = list(every)
            read = {c.table for c in value.find_all(exp.Column)}
            for side, columns in joined.items():
                if side in read:
                    extra += columns
            if extra and isinstance(projection, exp.Alias):
                projection.set(
                    "this", exp.Tuple(expressions=[value.copy(), *(c.copy() for c in extra)])
                )


def catalogue_from_dbt(catalog: Mapping[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    """The warehouse's columns from dbt's ``catalog.json`` (``dbt docs generate``), any adapter."""
    found: dict[str, dict[str, dict[str, str]]] = {}
    for section in ("nodes", "sources"):
        for entry in (catalog.get(section) or {}).values():
            meta = entry.get("metadata") or {}
            columns = {
                str(name): str((column or {}).get("type") or "")
                for name, column in (entry.get("columns") or {}).items()
            }
            found.setdefault(str(meta.get("schema", "")), {})[str(meta.get("name", ""))] = columns
    return found


def clickhouse_catalogue(client: Any, databases: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    """The columns of every table and view in ``databases``, from ClickHouse's catalogue."""
    found: dict[str, dict[str, dict[str, str]]] = {}
    for database, table, column, kind in client.query(
        "SELECT database, table, name, type FROM system.columns WHERE database IN %(dbs)s",
        parameters={"dbs": databases},
    ).result_rows:
        found.setdefault(database, {}).setdefault(table, {})[column] = kind
    return found


__all__ = [
    "Catalogue",
    "ColumnLineage",
    "LineageUnavailable",
    "RawColumn",
    "catalogue_from_dbt",
    "clickhouse_catalogue",
]
