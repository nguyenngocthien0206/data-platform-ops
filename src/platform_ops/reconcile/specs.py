"""The pairs to reconcile, from ``config/reconcile.yaml`` and both catalogues.

Columns, their kinds and their scales are read from Postgres's
``information_schema`` (the legacy side defines what the data is) and checked
against ClickHouse's ``system.columns``. A report's upstream tables come from
dbt's lineage: the raw sources its mart is built from, which is how a report
discrepancy is explained (or not).

Both catalogues are read again every night, because a parallel run lasts
weeks and schemas move during it: a column added on both sides is compared
from the night it appears. A pair that no longer lines up (a table gone, a
legacy column with no counterpart, a key column dropped) is schema drift
(``load_pairs``): that pair fails its night with the reason, not a discrepancy
on every row, and the other pairs are compared as usual.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_ops.common.config import Settings
from platform_ops.metadata.lineage import build_graph, upstream
from platform_ops.metadata.manifest import load_manifest
from platform_ops.reconcile.schema import (
    DEFAULT_DECIMAL_SCALE,
    Column,
    Level,
    PairConfig,
    ReconcileConfig,
    TableSpec,
    kind_from_postgres,
    target_scale,
)


def _postgres_columns(
    connection: Any, schema: str, table: str
) -> list[tuple[str, str, int | None]]:
    return [
        (str(name), str(data_type), None if scale is None else int(scale))
        for name, data_type, scale in connection.execute(
            "SELECT column_name, data_type, numeric_scale FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
    ]


def _clickhouse_columns(client: Any, database: str, table: str) -> dict[str, str]:
    return dict(
        client.query(
            "SELECT name, type FROM system.columns WHERE database = %(db)s AND table = %(t)s",
            parameters={"db": database, "t": table},
        ).result_rows
    )


class SchemaDrift(ValueError):
    """A pair whose two sides no longer line up, column for column."""


@dataclass(frozen=True)
class Drift:
    """A pair that cannot be compared tonight, and why."""

    spec: TableSpec  # what is known without the catalogues: name, level, relations
    reason: str


def _spec(
    pair: PairConfig,
    level: Level,
    source_schema: str,
    target_database: str,
    connection: Any,
    client: Any,
    upstream_tables: tuple[str, ...] = (),
) -> TableSpec:
    legacy = _postgres_columns(connection, source_schema, pair.source)
    if not legacy:
        raise SchemaDrift(f"{source_schema}.{pair.source} does not exist in Postgres")
    new = _clickhouse_columns(client, target_database, pair.target)
    if not new:
        raise SchemaDrift(f"{target_database}.{pair.target} does not exist in ClickHouse")
    columns = []
    for name, data_type, pg_scale in legacy:
        if name in pair.skip:
            continue
        target = pair.rename.get(name, name)
        if target not in new:
            raise SchemaDrift(
                f"{pair.name}: column {name} has no counterpart {target} in ClickHouse"
            )
        kind = kind_from_postgres(data_type)
        scale = 0
        if kind == "decimal":
            scale = pair.scale.get(
                name, pg_scale or target_scale(new[target]) or DEFAULT_DECIMAL_SCALE
            )
        columns.append(Column(name, kind, scale, "" if target == name else target, new[target]))
    missing = [k for k in pair.key if k not in {c.name for c in columns}]
    if missing:
        raise SchemaDrift(f"{pair.name}: key columns {missing} are not compared")
    if pair.changed_at and pair.changed_at not in {c.name for c in columns if c.kind == "ts"}:
        raise SchemaDrift(f"{pair.name}: changed_at {pair.changed_at} is not a compared timestamp")
    return TableSpec(
        name=pair.name,
        level=level,
        key=tuple(pair.key),
        columns=tuple(columns),
        source=f'"{source_schema}"."{pair.source}"',
        target=f"`{target_database}`.`{pair.target}`",
        final=level == "table",
        upstream=upstream_tables,
        changed_at=pair.changed_at,
    )


def report_upstream(manifest_path: Path, mart: str) -> tuple[str, ...]:
    """The raw tables a mart is built from, through dbt's lineage."""
    return _raw_upstream(build_graph(load_manifest(manifest_path)), mart)


def _raw_upstream(graph: Any, mart: str) -> tuple[str, ...]:
    found = upstream(graph, f"model.company.{mart}")
    return tuple(sorted(n.split(".")[-1] for n in found if n.startswith("source.company.raw.")))


def _stub(
    pair: PairConfig,
    level: Level,
    source_schema: str,
    target_database: str,
    upstream_tables: tuple[str, ...] = (),
) -> TableSpec:
    """A pair as the configuration names it, without its columns."""
    return TableSpec(
        name=pair.name,
        level=level,
        key=tuple(pair.key),
        columns=(),
        source=f'"{source_schema}"."{pair.source}"',
        target=f"`{target_database}`.`{pair.target}`",
        final=level == "table",
        upstream=upstream_tables,
        changed_at=pair.changed_at,
    )


def load_pairs(
    settings: Settings,
    connection: Any,
    client: Any,
    manifest_path: Path,
    config_path: Path,
    reports_schema: str | None = None,
) -> tuple[list[TableSpec], list[Drift]]:
    """Every pair to reconcile tonight, tables first, and the ones that drifted.

    ``reports_schema`` is where the legacy reports are read: the reporting
    schema by default, the frozen one during the parallel run (``asof``).
    """
    config = ReconcileConfig.load(config_path)
    company = settings.company
    app = company.name(company.legacy_schema)
    reporting = company.name(reports_schema or company.reporting_schema)
    raw = company.name(company.raw_database)
    marts = company.name("marts")
    graph = build_graph(load_manifest(manifest_path)) if config.reports else None
    wanted: list[tuple[PairConfig, Level, str, str, tuple[str, ...]]]
    wanted = [(p, "table", app, raw, ()) for p in config.tables]
    wanted += [
        (p, "report", reporting, marts, _raw_upstream(graph, p.target)) for p in config.reports
    ]
    specs: list[TableSpec] = []
    drifted: list[Drift] = []
    for pair, level, source, target, lineage in wanted:
        try:
            specs.append(_spec(pair, level, source, target, connection, client, lineage))
        except SchemaDrift as drift:
            drifted.append(Drift(_stub(pair, level, source, target, lineage), str(drift)))
    return specs, drifted


def load_specs(
    settings: Settings,
    connection: Any,
    client: Any,
    manifest_path: Path,
    config_path: Path,
    reports_schema: str | None = None,
) -> list[TableSpec]:
    """Every pair to reconcile, tables first; raises on the first pair that drifted."""
    specs, drifted = load_pairs(
        settings, connection, client, manifest_path, config_path, reports_schema
    )
    if drifted:
        raise SchemaDrift(drifted[0].reason)
    return specs
