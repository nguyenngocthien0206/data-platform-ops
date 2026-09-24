"""The legacy tables being migrated, described once for every engine.

Each column has a logical kind. The kind decides how a value is stored in each
engine (the DDL below) and how it is rendered before hashing
(``canonical.py``). The migration target stores some columns differently on
purpose: that difference is exactly what the reconciliation has to see through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa

Kind = Literal["int", "decimal", "text", "bool", "local_ts", "utc_ts"]
Dialect = Literal["postgres", "sqlserver", "duckdb"]

# SQL Server stores text as UTF-8 in this collation, so its bytes (and so its
# hashes) match Postgres and DuckDB. CI because the legacy system is
# case-insensitive, which the canonical case folding models (ADR 0009).
SQLSERVER_COLLATION = "Latin1_General_100_CI_AS_SC_UTF8"


@dataclass(frozen=True)
class Column:
    name: str
    kind: Kind
    precision: int = 0
    scale: int = 0
    length: int = 0

    def ddl_type(self, dialect: Dialect) -> str:
        if self.kind == "int":
            return "BIGINT"
        if self.kind == "decimal":
            number = "NUMERIC" if dialect == "postgres" else "DECIMAL"
            return f"{number}({self.precision}, {self.scale})"
        if self.kind == "text":
            if dialect == "sqlserver":
                return f"VARCHAR({self.length}) COLLATE {SQLSERVER_COLLATION}"
            return f"VARCHAR({self.length})"
        if self.kind == "bool":
            return "BIT" if dialect == "sqlserver" else "BOOLEAN"
        if self.kind == "local_ts":
            return "DATETIME2(6)" if dialect == "sqlserver" else "TIMESTAMP"
        return {"postgres": "TIMESTAMPTZ", "sqlserver": "DATETIMEOFFSET(6)"}.get(
            dialect, "TIMESTAMPTZ"
        )


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: str
    columns: tuple[Column, ...]

    @property
    def value_columns(self) -> tuple[Column, ...]:
        """Every column except the key, in table order."""
        return tuple(c for c in self.columns if c.name != self.key)

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(f"{self.name}.{name}")

    def ddl(self, dialect: Dialect, qualified_name: str) -> str:
        body = ",\n    ".join(
            f"{c.name} {c.ddl_type(dialect)}"
            + (" NOT NULL PRIMARY KEY" if c.name == self.key else "")
            for c in self.columns
        )
        return f"CREATE TABLE {qualified_name} (\n    {body}\n)"


def _text(name: str, length: int) -> Column:
    return Column(name, "text", length=length)


def _decimal(name: str, precision: int, scale: int) -> Column:
    return Column(name, "decimal", precision=precision, scale=scale)


CUSTOMERS = TableSpec(
    "customers",
    "customer_id",
    (
        Column("customer_id", "int"),
        _text("first_name", 40),
        _text("last_name", 40),
        _text("email", 120),
        _text("company_name", 80),
        _text("country", 2),
        Column("is_active", "bool"),
        _decimal("credit_limit", 12, 2),
        Column("created_local", "local_ts"),
        Column("updated_at", "utc_ts"),
    ),
)

ORDERS = TableSpec(
    "orders",
    "order_id",
    (
        Column("order_id", "int"),
        Column("customer_id", "int"),
        _text("status", 20),
        _text("channel", 20),
        _text("currency", 3),
        _decimal("amount", 20, 6),
        _decimal("fx_rate", 18, 8),
        Column("is_gift", "bool"),
        Column("ordered_local", "local_ts"),
        Column("created_at", "utc_ts"),
    ),
)

PAYMENTS = TableSpec(
    "payments",
    "payment_id",
    (
        Column("payment_id", "int"),
        Column("order_id", "int"),
        _text("method", 20),
        _text("status", 20),
        _decimal("amount", 12, 2),
        _decimal("fee", 10, 4),
        Column("is_refund", "bool"),
        Column("paid_local", "local_ts"),
        Column("settled_at", "utc_ts"),
    ),
)

TABLES: tuple[TableSpec, ...] = (CUSTOMERS, ORDERS, PAYMENTS)
TABLES_BY_NAME = {t.name: t for t in TABLES}


def arrow_schema(table: TableSpec) -> pa.Schema:
    """The Arrow types a legacy table is exchanged in, matching its DDL."""
    fields = []
    for c in table.columns:
        if c.kind == "int":
            arrow_type: pa.DataType = pa.int64()
        elif c.kind == "decimal":
            arrow_type = pa.decimal128(c.precision, c.scale)
        elif c.kind == "bool":
            arrow_type = pa.bool_()
        elif c.kind == "local_ts":
            arrow_type = pa.timestamp("us")
        elif c.kind == "utc_ts":
            arrow_type = pa.timestamp("us", tz="UTC")
        else:
            arrow_type = pa.string()
        fields.append(pa.field(c.name, arrow_type))
    return pa.schema(fields)
