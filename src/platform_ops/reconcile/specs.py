"""The pairs to reconcile, from ``config/reconcile.yaml`` and both catalogues.

Columns, their kinds and their scales are read from Postgres's
``information_schema`` (the legacy side defines what the data is) and checked
against ClickHouse's ``system.columns``; a column missing on the new side is an
error before any night is reconciled, not a discrepancy on every row. A
report's upstream tables come from dbt's lineage: the raw sources its mart is
built from, which is how a report discrepancy is explained (or not).
"""

from __future__ import annotations

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
        raise ValueError(f"{source_schema}.{pair.source} does not exist in Postgres")
    new = _clickhouse_columns(client, target_database, pair.target)
    if not new:
        raise ValueError(f"{target_database}.{pair.target} does not exist in ClickHouse")
    columns = []
    for name, data_type, pg_scale in legacy:
        if name in pair.skip:
            continue
        target = pair.rename.get(name, name)
        if target not in new:
            raise ValueError(
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
        raise ValueError(f"{pair.name}: key columns {missing} are not compared")
    if pair.changed_at and pair.changed_at not in {c.name for c in columns if c.kind == "ts"}:
        raise ValueError(f"{pair.name}: changed_at {pair.changed_at} is not a compared timestamp")
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
    graph = build_graph(load_manifest(manifest_path))
    found = upstream(graph, f"model.company.{mart}")
    return tuple(sorted(n.split(".")[-1] for n in found if n.startswith("source.company.raw.")))


def load_specs(
    settings: Settings,
    connection: Any,
    client: Any,
    manifest_path: Path,
    config_path: Path,
    reports_schema: str | None = None,
) -> list[TableSpec]:
    """Every pair to reconcile, tables first, in the order the configuration lists them.

    ``reports_schema`` is where the legacy reports are read: the reporting
    schema by default, the frozen one during the parallel run (``asof``).
    """
    config = ReconcileConfig.load(config_path)
    company = settings.company
    app = company.name(company.legacy_schema)
    reporting = company.name(reports_schema or company.reporting_schema)
    raw = company.name(company.raw_database)
    marts = company.name("marts")
    specs = [_spec(p, "table", app, raw, connection, client) for p in config.tables]
    for pair in config.reports:
        lineage = report_upstream(manifest_path, pair.target)
        specs.append(_spec(pair, "report", reporting, marts, connection, client, lineage))
    return specs
