"""``platform-ops reconcile run``: the migration scenario, the diff and the sign-off.

For each legacy engine (Postgres by default):

1. Generate the legacy data and load it into the engine, fresh every run.
2. Pass one, the job as delivered: migrate into Iceberg with its four defects,
   plant the target faults, diff, classify, and judge against the thresholds.
3. Pass two, the job fixed: the same with the defects corrected, so only the
   planted one-off faults remain. This is the re-run a team does after fixing
   what the first pass found, and where the segmented diff pays off.

Everything is written to ``ops.reconcile_*`` and to ``reports/reconciliation.md``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from platform_ops.common.config import Engine, Settings
from platform_ops.common.db import OPS_SCHEMA, open_connection, transaction
from platform_ops.reconcile import legacy
from platform_ops.reconcile.canonical import Rule, rules_for
from platform_ops.reconcile.classify import classify
from platform_ops.reconcile.connectors import (
    DuckDBConnector,
    PostgresConnector,
    SqlServerConnector,
    postgres_params,
    sqlserver_params,
)
from platform_ops.reconcile.diff import TableDiff, diff_table
from platform_ops.reconcile.metrics import Discrepancy, Grade, TableVerdict, grade, verdict
from platform_ops.reconcile.migrate import (
    TruthCell,
    data_files,
    migrate,
    namespace_for,
    open_catalog,
    reset_warehouse,
)
from platform_ops.reconcile.schema import TABLES, TABLES_BY_NAME, TableSpec
from platform_ops.simulation import migration_faults

PASSES: tuple[tuple[str, bool], ...] = (("as_delivered", False), ("fixed", True))


class ReconcileError(RuntimeError):
    """The reconciliation could not run, for a reason the user can fix."""


@dataclass
class PassResult:
    engine: str
    name: str
    diffs: dict[str, TableDiff]
    discrepancies: list[Discrepancy]
    truth: list[TruthCell]
    verdicts: dict[str, TableVerdict]
    grade: Grade

    @property
    def passed(self) -> bool:
        return all(v.passed for v in self.verdicts.values())


@dataclass
class ReconcileResult:
    engines: list[str]
    rules: dict[str, dict[str, dict[str, Rule]]]
    rows: dict[str, int]
    passes: list[PassResult] = field(default_factory=list)

    def signed_off(self) -> bool:
        """The delivered migration passes the thresholds on every engine."""
        return all(p.passed for p in self.passes if p.name == "as_delivered")


def _legacy_connector(engine: Engine, settings: Settings) -> Any:
    r = settings.reconcile
    env_file = settings.root / ".env"
    if engine == "duckdb":
        return DuckDBConnector(duckdb.connect(), local_zone=r.legacy_timezone)
    if engine == "postgres":
        params = postgres_params(env_file)
        try:
            return PostgresConnector(params, local_zone=r.legacy_timezone)
        except Exception as error:  # noqa: BLE001 - reported with the fix
            raise ReconcileError(
                f"Postgres is not reachable at {params.host}:{params.port} "
                f"({type(error).__name__}). Start it with `make up`."
            ) from error
    try:
        import pymssql  # noqa: F401
    except ImportError as error:
        raise ReconcileError(
            "SQL Server needs the optional driver: `uv sync --extra sqlserver`."
        ) from error
    mssql = sqlserver_params(env_file)
    try:
        return SqlServerConnector(
            mssql,
            local_zone=r.legacy_timezone,
            zone_names={r.legacy_timezone: r.legacy_timezone_windows},
        )
    except Exception as error:  # noqa: BLE001 - reported with the fix
        raise ReconcileError(
            f"SQL Server is not reachable at {mssql.host}:{mssql.port} "
            f"({type(error).__name__}). Start it with `docker compose --profile sqlserver up -d`."
        ) from error


def _target(catalog: Any, namespace: str) -> DuckDBConnector:
    """The migrated Iceberg tables, read by DuckDB from the files PyIceberg plans."""
    con = duckdb.connect()
    relations = {}
    for spec in TABLES:
        files = data_files(catalog.load_table(f"{namespace}.{spec.name}"))
        view = f"target_{spec.name}"
        con.execute(f"CREATE VIEW {view} AS SELECT * FROM read_parquet({files!r})")
        relations[spec.name] = view
    return DuckDBConnector(con, engine="iceberg", relations=relations)


class _ExportOnce:
    """The legacy store, exporting each table once for both passes.

    The legacy data does not change between the passes, so the second
    migration reads the first one's export instead of pulling it again.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.exports: dict[str, pa.Table] = {}

    def recreate(self, tables: dict[str, pa.Table], specs: Sequence[TableSpec]) -> None:
        self.exports.clear()
        self.store.recreate(tables, specs)

    def export(self, table: TableSpec, work_dir: Path) -> pa.Table:
        if table.name not in self.exports:
            self.exports[table.name] = self.store.export(table, work_dir)
        return self.exports[table.name]


def _discrepancies(diff: TableDiff, tolerance: Decimal) -> list[Discrepancy]:
    spec = TABLES_BY_NAME[diff.table]
    found = [Discrepancy(diff.table, k, "", "missing_in_target", "", "") for k in diff.missing]
    found += [Discrepancy(diff.table, k, "", "extra_in_target", "", "") for k in diff.extra]
    found += [
        Discrepancy(diff.table, c.key, c.column,
                    classify(spec.column(c.column), c.source, c.target, tolerance),
                    c.source, c.target)
        for c in diff.cells
    ]  # fmt: skip
    return sorted(found, key=lambda d: (d.key, d.column))


def run_reconcile(settings: Settings, engines: Sequence[Engine] | None = None) -> ReconcileResult:
    r = settings.reconcile
    chosen: list[Engine] = list(engines or r.engines)
    tolerance = Decimal(str(r.rounding_tolerance))
    zone_names = {r.legacy_timezone: r.legacy_timezone_windows}
    warehouse = settings.resolve(settings.paths.iceberg_warehouse) / "reconcile"
    reset_warehouse(warehouse)
    catalog = open_catalog(warehouse)
    data = legacy.generate(settings)
    result = ReconcileResult(
        engines=[str(e) for e in chosen],
        rules={e: {t.name: rules_for(t, e, r) for t in TABLES} for e in chosen},
        rows={name: table.num_rows for name, table in data.items()},
    )
    for engine in chosen:
        store = _legacy_connector(engine, settings)
        try:
            exports = _ExportOnce(store)
            exports.recreate(data, TABLES)
            rules = result.rules[engine]
            for name, fixed in PASSES:
                with tempfile.TemporaryDirectory() as work:
                    migration = migrate(exports, catalog, engine, r.legacy_timezone, Path(work),
                                        fixed=fixed)  # fmt: skip
                truth = migration.truth + migration_faults.inject(
                    migration.tables, migration.truth, settings.seed
                )
                target = _target(catalog, namespace_for(engine, fixed))
                diffs = {
                    spec.name: diff_table(spec, store, target, rules[spec.name], fanout=r.fanout,
                                          leaf_width=r.leaf_width, zone_names=zone_names)
                    for spec in TABLES
                }  # fmt: skip
                target.connection.close()
                found = [d for diff in diffs.values() for d in _discrepancies(diff, tolerance)]
                result.passes.append(
                    PassResult(
                        engine=engine,
                        name=name,
                        diffs=diffs,
                        discrepancies=found,
                        truth=truth,
                        verdicts={
                            s.name: verdict(s, diffs[s.name], found, r.thresholds) for s in TABLES
                        },
                        grade=grade(truth, found, TABLES_BY_NAME, rules),
                    )
                )
        finally:
            closer = getattr(store, "close", None)
            if closer is not None:
                closer()
            elif isinstance(store, DuckDBConnector):
                store.connection.close()
    return result


# -- persistence -----------------------------------------------------------------------


def _replace(connection: duckdb.DuckDBPyConnection, name: str, table: pa.Table) -> None:
    """Replace ``ops.<name>`` with ``table``. Arrow, because these run to 400k rows."""
    connection.register("_rows", table)
    connection.execute(f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.{name} AS SELECT * FROM _rows")
    connection.unregister("_rows")


def _columns(
    rows: list[tuple[Any, ...]], names: Sequence[str], types: Sequence[pa.DataType]
) -> pa.Table:
    arrays = [pa.array([row[i] for row in rows], type=t) for i, t in enumerate(types)]
    return pa.Table.from_arrays(arrays, names=list(names))


def persist(connection: duckdb.DuckDBPyConnection, result: ReconcileResult) -> None:
    s, i, f, b = pa.string(), pa.int64(), pa.float64(), pa.bool_()
    truth, found, tables, segments, metrics = [], [], [], [], []
    for p in result.passes:
        truth += [(p.engine, p.name, c.table, c.key, c.column, c.klass, c.source, c.label)
                  for c in p.truth]  # fmt: skip
        found += [(p.engine, p.name, d.table, d.key, d.column, d.klass, d.source_value,
                   d.target_value) for d in p.discrepancies]  # fmt: skip
        for name, diff in p.diffs.items():
            v = p.verdicts[name]
            tables.append((p.engine, p.name, name, diff.source_rows, diff.target_rows,
                           v.row_match_rate, v.passed, diff.summary_rows, diff.fetched_rows,
                           diff.naive_transferred, diff.queries))  # fmt: skip
            segments += [
                (p.engine, p.name, name, level, width, compared, differing)
                for level, (width, compared, differing) in enumerate(diff.levels)
            ]
            metrics += [(p.engine, p.name, "column_match_rate", f"{name}.{c}", rate)
                        for c, rate in v.column_match_rates.items()]  # fmt: skip
        g = p.grade
        metrics += [
            (p.engine, p.name, "recall", "all", g.recall),
            (p.engine, p.name, "precision", "all", g.precision),
            (p.engine, p.name, "classification_accuracy", "all", g.classification_accuracy),
            (p.engine, p.name, "expected", "all", float(g.expected)),
            (p.engine, p.name, "detected", "all", float(g.detected)),
            (p.engine, p.name, "equivalent_under_policy", "all", float(g.equivalent_under_policy)),
        ]
        metrics += [(p.engine, p.name, "found_rate", label, found_n / planted if planted else 1.0)
                    for label, (planted, found_n) in g.by_label.items()]  # fmt: skip
    with transaction(connection):
        _replace(connection, "reconcile_ground_truth", _columns(truth,
                 ("engine", "pass", "table_name", "key", "column_name", "class", "source", "label"),
                 (s, s, s, i, s, s, s, s)))  # fmt: skip
        _replace(connection, "reconcile_discrepancies", _columns(found,
                 ("engine", "pass", "table_name", "key", "column_name", "class", "source_value",
                  "target_value"), (s, s, s, i, s, s, s, s)))  # fmt: skip
        _replace(connection, "reconcile_tables", _columns(tables,
                 ("engine", "pass", "table_name", "source_rows", "target_rows", "row_match_rate",
                  "passed", "summary_rows", "fetched_rows", "naive_rows", "queries"),
                 (s, s, s, i, i, f, b, i, i, i, i)))  # fmt: skip
        _replace(connection, "reconcile_segments", _columns(segments,
                 ("engine", "pass", "table_name", "level", "width", "segments", "differing"),
                 (s, s, s, i, i, i, i)))  # fmt: skip
        _replace(connection, "reconcile_metrics", _columns(metrics,
                 ("engine", "pass", "metric", "dimension", "value"), (s, s, s, s, f)))  # fmt: skip


@dataclass(frozen=True)
class ReconcileSummary:
    engines: list[str]
    signed_off: bool
    report_path: Path
    recall: float
    classification_accuracy: float


def run_and_report(
    settings: Settings, engines: Sequence[Engine] | None = None
) -> tuple[ReconcileSummary, ReconcileResult]:
    from platform_ops.reconcile.report import write_report

    result = run_reconcile(settings, engines)
    with open_connection(settings) as connection:
        persist(connection, result)
    report_path = write_report(settings, result)
    delivered = [p for p in result.passes if p.name == "as_delivered"]
    return (
        ReconcileSummary(
            engines=result.engines,
            signed_off=result.signed_off(),
            report_path=report_path,
            recall=min(p.grade.recall for p in delivered),
            classification_accuracy=min(p.grade.classification_accuracy for p in delivered),
        ),
        result,
    )
