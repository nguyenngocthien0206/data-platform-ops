"""The two sides of a reconciliation: the legacy Postgres and ClickHouse.

A connector answers the only three questions the segmented diff asks, and
computes every answer inside its engine: how many rows a table has, the row
count and hash sums per segment of the key's MD5, and the canonical values of
the rows in a few segments (CLAUDE.md: push computation into the database).
Only summaries and the rows of differing leaf segments cross the wire.

Postgres hashes each row once into a temporary table and groups that small
table at every level, because rendering a row canonically is the expensive part
(1.x measured 53 against 20 seconds). ClickHouse renders and groups on the fly:
it is columnar and parallel, and the reconciliation reads it as the platform
team's user, which is read-only (ADR 0013).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from platform_ops.reconcile.canonical import Dialect, RenderedTable
from platform_ops.reconcile.schema import TableSpec


@dataclass(frozen=True)
class Summary:
    rows: int
    h1: int
    h2: int


class Connector(Protocol):
    engine: str
    dialect: Dialect

    def summarize(
        self, table: TableSpec, rendered: RenderedTable, depth: int, parents: Sequence[int]
    ) -> dict[int, Summary]: ...

    def fetch(
        self, table: TableSpec, rendered: RenderedTable, depth: int, buckets: Sequence[int]
    ) -> dict[str, tuple[str, ...]]: ...

    def forget(self) -> None: ...


def _shift(depth: int) -> int:
    return 32 - 4 * depth


class PostgresConnector:
    """The legacy side. ``connection`` is a psycopg connection in autocommit, UTC."""

    engine = "postgres"
    dialect: Dialect = "postgres"

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._prepared: dict[str, str] = {}

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cursor = self.connection.execute(sql, list(params))
        return cursor.fetchall() if cursor.description else []

    def _hashes(self, table: TableSpec, rendered: RenderedTable) -> str:
        name = f"rowhash_{table.level}_{table.name}"
        if self._prepared.get(name) != rendered.row:
            self._rows(f"DROP TABLE IF EXISTS {name}")
            self._rows(
                f"CREATE TEMP TABLE {name} AS SELECT {rendered.bucket} AS bucket, "
                f"{rendered.h1} AS h1, {rendered.h2} AS h2 FROM {table.source}"
            )
            self._prepared[name] = rendered.row
        return name

    def forget(self) -> None:
        """Drop the hashed rows, so the next night hashes the tables as they are then."""
        for name in self._prepared:
            self._rows(f"DROP TABLE IF EXISTS {name}")
        self._prepared = {}

    def summarize(
        self, table: TableSpec, rendered: RenderedTable, depth: int, parents: Sequence[int]
    ) -> dict[int, Summary]:
        hashes = self._hashes(table, rendered)
        bucket = f"(bucket >> {_shift(depth)})"
        where, params = "", list[Any]()
        if depth > 1:
            where = f"WHERE (bucket >> {_shift(depth - 1)}) IN (SELECT unnest(%s::bigint[]))"
            params = [list(parents)]
        rows = self._rows(
            f"SELECT {bucket}, count(*), sum(h1), sum(h2) FROM {hashes} {where} GROUP BY 1",
            params,
        )
        return {int(b): Summary(int(n), int(s1), int(s2)) for b, n, s1, s2 in rows}

    def fetch(
        self, table: TableSpec, rendered: RenderedTable, depth: int, buckets: Sequence[int]
    ) -> dict[str, tuple[str, ...]]:
        values = ", ".join(rendered.values) or "''"
        rows = self._rows(
            f"SELECT {rendered.key}, {values} FROM {table.source} "
            f"WHERE ({rendered.bucket} >> {_shift(depth)}) IN (SELECT unnest(%s::bigint[]))",
            [list(buckets)],
        )
        return {str(row[0]): tuple(str(v) for v in row[1:]) for row in rows}


class ClickHouseConnector:
    """The new side. ``client`` is a clickhouse-connect client."""

    engine = "clickhouse"
    dialect: Dialect = "clickhouse"

    def __init__(self, client: Any) -> None:
        self.client = client

    def _relation(self, table: TableSpec) -> str:
        return table.target + (" FINAL" if table.final else "")

    def forget(self) -> None:
        return None

    def summarize(
        self, table: TableSpec, rendered: RenderedTable, depth: int, parents: Sequence[int]
    ) -> dict[int, Summary]:
        where = ""
        if depth > 1:
            members = ", ".join(str(p) for p in parents)
            where = f"WHERE bitShiftRight(bucket, {_shift(depth - 1)}) IN ({members})"
        rows = self.client.query(
            f"SELECT bitShiftRight(bucket, {_shift(depth)}) AS b, count(), "
            f"sum(toUInt64(h1)), sum(toUInt64(h2)) FROM ("
            f"SELECT {rendered.bucket} AS bucket, {rendered.h1} AS h1, {rendered.h2} AS h2 "
            f"FROM {self._relation(table)}) {where} GROUP BY b"
        ).result_rows
        return {int(b): Summary(int(n), int(s1), int(s2)) for b, n, s1, s2 in rows}

    def fetch(
        self, table: TableSpec, rendered: RenderedTable, depth: int, buckets: Sequence[int]
    ) -> dict[str, tuple[str, ...]]:
        values = ", ".join(rendered.values) or "''"
        members = ", ".join(str(b) for b in buckets)
        rows = self.client.query(
            f"SELECT {rendered.key}, {values} FROM {self._relation(table)} "
            f"WHERE bitShiftRight({rendered.bucket}, {_shift(depth)}) IN ({members})"
        ).result_rows
        return {str(row[0]): tuple(str(v) for v in row[1:]) for row in rows}
