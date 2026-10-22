"""What is reconciled: pairs of relations, their key, and each column's kind (version 2.0).

``config/reconcile.yaml`` declares the pairs: the company's operational tables
(legacy Postgres ``app`` against ClickHouse ``raw``) and its reports (Postgres
``reporting`` against the dbt marts). The columns are not typed by hand: they
are read from Postgres's own catalogue and mapped to a logical kind, which
decides how a value is rendered before hashing (``canonical.py``). ClickHouse's
physical types are read too, because a few of them need their own rendering
(``FixedString`` padding, an integer measure stored as a ``Decimal``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["int", "decimal", "text", "bool", "ts", "date"]
Level = Literal["table", "report"]


@dataclass(frozen=True)
class Column:
    name: str
    kind: Kind
    scale: int = 0
    # The column's name on the ClickHouse side, when it differs.
    target_name: str = ""
    # The column's physical type on the ClickHouse side.
    target_type: str = ""

    @property
    def target(self) -> str:
        return self.target_name or self.name


@dataclass(frozen=True)
class TableSpec:
    name: str
    level: Level
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    source: str  # a qualified Postgres relation
    target: str  # a qualified ClickHouse relation
    # Read the target with FINAL: a ReplacingMergeTree keeps a row twice until
    # it merges, and only the latest version is the copy.
    final: bool = False
    # For a report: the operational tables its mart is built from (lineage).
    upstream: tuple[str, ...] = field(default=())
    # The legacy column that records when a row last changed. With a
    # watermark, the legacy side is compared as of it: rows changed later are
    # left for the next night (the copy has not read them yet).
    changed_at: str = ""
    # What the legacy side is as of tonight: the copy's watermark for a table,
    # the time its snapshot was frozen for a report. None compares it as it is.
    watermark: datetime | None = None

    @property
    def value_columns(self) -> tuple[Column, ...]:
        """Every column except the key, in table order."""
        return tuple(c for c in self.columns if c.name not in self.key)

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)


def kind_from_postgres(data_type: str) -> Kind:
    """The logical kind of a Postgres column type (``information_schema`` spelling)."""
    if data_type in ("bigint", "integer", "smallint"):
        return "int"
    if data_type in ("numeric", "double precision", "real"):
        return "decimal"
    if data_type == "boolean":
        return "bool"
    if data_type.startswith("timestamp"):
        return "ts"
    if data_type == "date":
        return "date"
    return "text"


# -- the declaration in config/reconcile.yaml ----------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairConfig(_Strict):
    """One pair to reconcile. ``source`` and ``target`` are relation names without
    their (namespaced) schema; ``rename`` maps source column names to target ones."""

    name: str
    source: str
    target: str
    key: list[str] = Field(min_length=1)
    rename: dict[str, str] = Field(default_factory=dict)
    # Columns whose decimals are compared at this scale (default: their own).
    scale: dict[str, int] = Field(default_factory=dict)
    # Columns left out of the comparison, each with the reason in the YAML.
    skip: list[str] = Field(default_factory=list)
    # The legacy column that records when a row last changed (the one the copy
    # reads by), so a night compares the table as of the copy's watermark.
    changed_at: str = ""


class ReconcileConfig(_Strict):
    tables: list[PairConfig]
    reports: list[PairConfig]

    @classmethod
    def load(cls, path: Path) -> ReconcileConfig:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


DEFAULT_DECIMAL_SCALE = 2
_SCALE = re.compile(r"Decimal\(\d+,\s*(\d+)\)")


def target_scale(clickhouse_type: str) -> int | None:
    found = _SCALE.search(clickhouse_type)
    return int(found[1]) if found else None
