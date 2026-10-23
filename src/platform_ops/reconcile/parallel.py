"""The parallel run's nightly reconciliation, and the cutover rule (Phase 12, ADR 0014).

Every night of the parallel run, after the company's ingestion and dbt build,
``ParallelRun`` compares both levels the migration has to sign off:

- the 9 operational tables, legacy Postgres against ClickHouse's raw layer,
  graded against the ground truth of what was planted that night;
- the 12 legacy reports against the dbt marts that replace them. A report
  difference is explained when the columns it reads differ upstream that night
  (column lineage from dbt's compiled SQL, ``explain.py``); one that is not is
  a logic difference between the old SQL and the new, the most important thing
  a parallel run can find.

The legacy side keeps taking writes after the copy has read it, so a night
compares it as of the copy: each table as of its watermark (``Night.watermarks``,
by its ``changed_at`` column), each report as frozen just before the copy read
(``freeze``, ``asof.py``). What was written since waits for the next night, and
a pair whose legacy side is more than ``max_watermark_lag_hours`` behind fails.

Pairs are compared ``reconcile.workers`` at a time, each with its own Postgres
connection and its own ClickHouse client, as the platform team's read-only
user. Every night is recorded in the ops store. The cutover is recommended on
the night that completes ``reconcile.clean_nights_for_cutover`` consecutive
nights with every table and every report within the thresholds.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from platform_ops.common.config import DISCREPANCY_CLASSES, Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.logging import get_logger
from platform_ops.common.servers import clickhouse_client, connect_postgres
from platform_ops.common.store import open_store, replace_ops_table
from platform_ops.common.tagging import Tag
from platform_ops.metadata.column_lineage import ColumnLineage, LineageUnavailable, RawColumn
from platform_ops.reconcile.asof import freeze_reports
from platform_ops.reconcile.canonical import Rule, rules_for
from platform_ops.reconcile.classify import classify
from platform_ops.reconcile.connectors import ClickHouseConnector, PostgresConnector
from platform_ops.reconcile.diff import TableDiff, diff_table
from platform_ops.reconcile.explain import Explanation, explain
from platform_ops.reconcile.metrics import Discrepancy, Grade, TableVerdict, grade, verdict
from platform_ops.reconcile.schema import PairConfig, ReconcileConfig, TableSpec
from platform_ops.reconcile.specs import Drift, load_pairs
from platform_ops.simulation.access import DBT_LAYERS
from platform_ops.simulation.company import Night

logger = get_logger("reconcile")

NIGHTS_DDL = """
    night integer NOT NULL, run_at timestamp NOT NULL, job text NOT NULL,
    backfilled boolean NOT NULL, planted text NOT NULL, repaired text NOT NULL,
    level text NOT NULL, name text NOT NULL,
    source_rows bigint NOT NULL, target_rows bigint NOT NULL,
    missing bigint NOT NULL, extra bigint NOT NULL, duplicate_key bigint NOT NULL,
    in_flight bigint NOT NULL, after_watermark bigint NOT NULL, rows_with_diffs bigint NOT NULL,
    rounding bigint NOT NULL, timezone_shift bigint NOT NULL, whitespace bigint NOT NULL,
    case_only bigint NOT NULL, value_mismatch bigint NOT NULL,
    row_match_rate double precision NOT NULL, passed boolean NOT NULL, failures text NOT NULL,
    rows_moved bigint NOT NULL, rows_moved_naive bigint NOT NULL,
    differing_upstream text NOT NULL, explained boolean, traced boolean"""
NIGHTS_COLUMNS = [
    "night",
    "run_at",
    "job",
    "backfilled",
    "planted",
    "repaired",
    "level",
    "name",
    "source_rows",
    "target_rows",
    "missing",
    "extra",
    "duplicate_key",
    "in_flight",
    "after_watermark",
    "rows_with_diffs",
    "rounding",
    "timezone_shift",
    "whitespace",
    "case_only",
    "value_mismatch",
    "row_match_rate",
    "passed",
    "failures",
    "rows_moved",
    "rows_moved_naive",
    "differing_upstream",
    "explained",
    "traced",
]
EXAMPLES_DDL = """
    night integer NOT NULL, level text NOT NULL, name text NOT NULL, key text NOT NULL,
    column_name text NOT NULL, class text NOT NULL, source_value text NOT NULL,
    target_value text NOT NULL"""
EXAMPLES_COLUMNS = [
    "night",
    "level",
    "name",
    "key",
    "column_name",
    "class",
    "source_value",
    "target_value",
]
GRADES_DDL = """
    night integer NOT NULL, expected bigint NOT NULL, detected bigint NOT NULL,
    true_positives bigint NOT NULL, correctly_classified bigint NOT NULL,
    equivalent_under_policy bigint NOT NULL"""
GRADES_COLUMNS = [
    "night",
    "expected",
    "detected",
    "true_positives",
    "correctly_classified",
    "equivalent_under_policy",
]
LABELS_DDL = """
    night integer NOT NULL, label text NOT NULL, planted bigint NOT NULL, found bigint NOT NULL"""
LABELS_COLUMNS = ["night", "label", "planted", "found"]
CUTOVER_DDL = """
    recommended boolean NOT NULL, night integer, run_at timestamp,
    clean_nights integer NOT NULL, required integer NOT NULL, parallel_nights integer NOT NULL"""
CUTOVER_COLUMNS = [
    "recommended",
    "night",
    "run_at",
    "clean_nights",
    "required",
    "parallel_nights",
]


@dataclass
class PairResult:
    spec: TableSpec
    diff: TableDiff
    discrepancies: list[Discrepancy]
    verdict: TableVerdict


@dataclass
class NightResult:
    night: Night
    pairs: list[PairResult]
    grade: Grade
    unexplained: list[str] = field(default_factory=list)
    explanations: dict[str, Explanation] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return all(p.verdict.passed for p in self.pairs) and not self.unexplained


@dataclass(frozen=True)
class Cutover:
    recommended: bool
    night: int | None
    run_at: datetime | None
    clean_nights: int
    required: int
    parallel_nights: int


def _rows(n: int) -> str:
    return f"{n} row" if n == 1 else f"{n} rows"


def discrepancies_of(
    spec: TableSpec, diff: TableDiff, tolerance: Decimal, time_zones: Sequence[str] = ()
) -> list[Discrepancy]:
    found = [Discrepancy(spec.name, k, "", "missing_in_target", "", "") for k in diff.missing]
    found += [Discrepancy(spec.name, k, "", "extra_in_target", "", "") for k in diff.extra]
    found += [
        Discrepancy(spec.name, d.key, "", "duplicate_key", _rows(d.source), _rows(d.target))
        for d in diff.duplicates
    ]
    found += [
        Discrepancy(
            spec.name,
            c.key,
            c.column,
            classify(spec.column(c.column), c.source, c.target, tolerance, time_zones),
            c.source,
            c.target,
        )
        for c in diff.cells
    ]
    return sorted(found, key=lambda d: (d.key, d.column))


def decide(results: list[NightResult], required: int) -> Cutover:
    """The first night that completes ``required`` consecutive clean nights, if any."""
    streak = 0
    for result in results:
        streak = streak + 1 if result.clean else 0
        if streak == required:
            n = result.night
            return Cutover(True, n.index, n.run_at, streak, required, len(results))
    return Cutover(False, None, None, streak, required, len(results))


class ParallelRun:
    """Called by the company's run after each night's build; records every night."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config_path = settings.root / "config" / "reconcile.yaml"
        self.specs: list[TableSpec] = []
        self.drifted: list[Drift] = []
        # Each report's mart column to the raw columns it reads, None when untraced.
        self.lineage: dict[str, dict[str, frozenset[RawColumn]] | None] = {}
        self.rules: dict[str, dict[str, Rule]] = {}
        self._started = False
        self.results: list[NightResult] = []
        self.tolerance = Decimal(str(settings.reconcile.rounding_tolerance))
        self._night_at = settings.simulation.start

    # -- the ops store --------------------------------------------------------------

    def _recreate_store(self) -> None:
        with open_store(self.settings) as store:
            for name, ddl, columns in (
                ("reconcile_nights", NIGHTS_DDL, NIGHTS_COLUMNS),
                ("reconcile_examples", EXAMPLES_DDL, EXAMPLES_COLUMNS),
                ("reconcile_grades", GRADES_DDL, GRADES_COLUMNS),
                ("reconcile_labels", LABELS_DDL, LABELS_COLUMNS),
                ("reconcile_cutover", CUTOVER_DDL, CUTOVER_COLUMNS),
            ):
                replace_ops_table(store, self.settings, name, ddl, columns, [])

    def _append(
        self, store: Any, name: str, columns: list[str], rows: list[tuple[Any, ...]]
    ) -> None:
        from psycopg import sql

        from platform_ops.common.store import ops_table

        if not rows:
            return
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
            ops_table(self.settings, name), sql.SQL(", ").join(map(sql.Identifier, columns))
        )
        with store.cursor().copy(statement) as copy:
            for row in rows:
                copy.write_row(row)

    # -- one night ----------------------------------------------------------------------

    def freeze(self, at: datetime) -> None:
        """Freeze the legacy reports, just before the copy reads up to ``at``."""
        company = self.settings.company
        reports: list[PairConfig] = ReconcileConfig.load(self.config_path).reports
        freeze_reports(
            lambda: connect_postgres(self.settings),
            company.name(company.reporting_schema),
            company.name(company.frozen_reporting_schema),
            [pair.source for pair in reports],
            workers=self.settings.reconcile.workers,
        )

    def _load_specs(self) -> None:
        """Read both catalogues and the manifest again: schemas move during a parallel run."""
        manifest = invocation_from_settings(self.settings).manifest_path
        with connect_postgres(self.settings) as connection:
            client = clickhouse_client(self.settings, "platform")
            try:
                self.specs, self.drifted = load_pairs(
                    self.settings,
                    connection,
                    client,
                    manifest,
                    self.config_path,
                    reports_schema=self.settings.company.frozen_reporting_schema,
                )
                self.lineage = self._trace(client, manifest)
            finally:
                client.close()
        self.rules = {s.name: rules_for(s, self.settings.reconcile) for s in self.specs}

    def _trace(self, client: Any, manifest_path: Path) -> dict[str, Any]:
        """Each report's column lineage, from tonight's compiled SQL and catalogue."""
        company = self.settings.company
        databases = [company.name(d) for d in (company.raw_database, *DBT_LAYERS)]
        catalogue: dict[str, dict[str, dict[str, str]]] = {}
        for database, table, column, kind in client.query(
            "SELECT database, table, name, type FROM system.columns WHERE database IN %(dbs)s",
            parameters={"dbs": databases},
        ).result_rows:
            catalogue.setdefault(database, {}).setdefault(table, {})[column] = kind
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        project = manifest["metadata"]["project_name"]
        traced = ColumnLineage(manifest, manifest_path.parent / "compiled" / project, catalogue)
        found: dict[str, Any] = {}
        for pair in ReconcileConfig.load(self.config_path).reports:
            try:
                columns = traced.model(f"model.{project}.{pair.target}")
            except LineageUnavailable as error:
                logger.warning("%s: explained by table tonight: %s", pair.name, error)
                columns = None
            found[pair.name] = columns
        return found

    def _as_of(self, spec: TableSpec, night: Night) -> TableSpec:
        """The pair as tonight compares it: a table as of the copy, a report as frozen.

        A table pair is named after its legacy table, which is how the copy
        records its watermark.
        """
        if spec.level == "report":
            return replace(spec, watermark=night.frozen_at)
        return replace(spec, watermark=night.watermarks.get(spec.name))

    def _compare(self, spec: TableSpec) -> PairResult:
        connection = connect_postgres(self.settings)
        connection.autocommit = True
        # The reconciliation's reads are the migration's own cost (Phase 13).
        tag = Tag(self._night_at, "reconcile", subject=f"{spec.level}.{spec.name}")
        client = clickhouse_client(self.settings, "platform", tag.settings())
        try:
            diff = diff_table(
                spec,
                PostgresConnector(connection),
                ClickHouseConnector(client),
                self.rules[spec.name],
                leaf_width=self.settings.reconcile.leaf_width,
            )
        finally:
            connection.close()
            client.close()
        found = discrepancies_of(spec, diff, self.tolerance, self.settings.reconcile.time_zones)
        thresholds = self.settings.reconcile.thresholds
        return PairResult(spec, diff, found, verdict(spec, diff, found, thresholds, self._night_at))

    def _drifted(self, drift: Drift) -> PairResult:
        """A pair that cannot be compared tonight: it fails, with the reason."""
        spec = drift.spec
        result = TableVerdict(spec.name, 0, 0, 0.0, {}, dict.fromkeys(DISCREPANCY_CLASSES, 0))
        result.failures.append(f"schema: {drift.reason}")
        return PairResult(spec, TableDiff(spec.name), [], result)

    def __call__(self, night: Night) -> None:
        self._night_at = night.run_at
        if not self._started:
            # On the first night, not before: nothing is touched until the
            # company's run has checked its passwords and built the marts.
            self._recreate_store()
            self._started = True
        self._load_specs()
        # The largest pairs first, so the pool is not left waiting on one at the end.
        tonight = [self._as_of(spec, night) for spec in self.specs]
        with ThreadPoolExecutor(self.settings.reconcile.workers) as pool:
            pairs = list(pool.map(self._compare, tonight))
        pairs += [self._drifted(drift) for drift in self.drifted]
        tables = [p for p in pairs if p.spec.level == "table"]
        found = [d for p in tables for d in p.discrepancies]
        specs = {p.spec.name: p.spec for p in tables}
        result = NightResult(
            night, pairs, grade(list(night.truth), found, specs, {n: self.rules[n] for n in specs})
        )
        # What differs upstream tonight: raw columns, and tables with rows
        # missing, extra or duplicated. A table that could not be compared may
        # differ in any way, so it counts as differing by row.
        cells = {
            (p.spec.name, p.spec.column(c.column).target) for p in tables for c in p.diff.cells
        }
        rows = {p.spec.name for p in tables if p.diff.missing or p.diff.extra or p.diff.duplicates}
        rows |= {d.spec.name for d in self.drifted if d.spec.level == "table"}
        for pair in pairs:
            if pair.spec.level != "report":
                continue
            why = explain(pair.spec, pair.diff, self.lineage.get(pair.spec.name), cells, rows)
            result.explanations[pair.spec.name] = why
            if not why.explained:
                result.unexplained.append(pair.spec.name)
        self.results.append(result)
        self._record(result)

    def _record(self, result: NightResult) -> None:
        n = result.night
        job = "delivered" if n.delivered else "fixed"
        nights, examples = [], []
        limit = self.settings.reconcile.examples_per_class
        for pair in result.pairs:
            d, v = pair.diff, pair.verdict
            why = result.explanations.get(pair.spec.name)
            nights.append(
                (
                    n.index,
                    n.run_at,
                    job,
                    n.backfilled,
                    n.planted,
                    n.repaired,
                    pair.spec.level,
                    pair.spec.name,
                    d.source_rows,
                    d.target_rows,
                    len(d.missing),
                    len(d.extra),
                    len(d.duplicates),
                    len(d.in_flight),
                    d.after_watermark,
                    d.rows_with_cell_diffs,
                    v.by_class["rounding"],
                    v.by_class["timezone_shift"],
                    v.by_class["whitespace"],
                    v.by_class["case_only"],
                    v.by_class["value_mismatch"],
                    v.row_match_rate,
                    v.passed,
                    "; ".join(v.failures),
                    d.transferred,
                    d.naive_transferred,
                    ", ".join(why.causes) if why else "",
                    why.explained if why else None,
                    why.traced if why else None,
                )
            )
            kept: Counter[str] = Counter()
            for item in pair.discrepancies:
                if kept[item.klass] < limit:
                    kept[item.klass] += 1
                    examples.append(
                        (
                            n.index,
                            pair.spec.level,
                            pair.spec.name,
                            item.key,
                            item.column,
                            item.klass,
                            item.source_value,
                            item.target_value,
                        )
                    )
        g = result.grade
        grades = [
            (
                n.index,
                g.expected,
                g.detected,
                g.true_positives,
                g.correctly_classified,
                g.equivalent_under_policy,
            )
        ]
        labels = [(n.index, label, p, f) for label, (p, f) in g.by_label.items()]
        with open_store(self.settings) as store:
            self._append(store, "reconcile_nights", NIGHTS_COLUMNS, nights)
            self._append(store, "reconcile_examples", EXAMPLES_COLUMNS, examples)
            self._append(store, "reconcile_grades", GRADES_COLUMNS, grades)
            self._append(store, "reconcile_labels", LABELS_COLUMNS, labels)

    # -- the end of the run ----------------------------------------------------------

    def finish(self) -> Cutover:
        """Decide the cutover and record it."""
        decision = decide(self.results, self.settings.reconcile.clean_nights_for_cutover)
        with open_store(self.settings) as store:
            replace_ops_table(
                store,
                self.settings,
                "reconcile_cutover",
                CUTOVER_DDL,
                CUTOVER_COLUMNS,
                [
                    (
                        decision.recommended,
                        decision.night,
                        decision.run_at,
                        decision.clean_nights,
                        decision.required,
                        decision.parallel_nights,
                    )
                ],
            )
        return decision


__all__ = ["DISCREPANCY_CLASSES", "Cutover", "NightResult", "ParallelRun", "Path", "decide"]
