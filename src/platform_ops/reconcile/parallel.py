"""The parallel run's nightly reconciliation, and the cutover rule (Phase 12, ADR 0014).

Every night of the parallel run, after the company's ingestion and dbt build,
``ParallelRun`` compares both levels the migration has to sign off:

- the 9 operational tables, legacy Postgres against ClickHouse's raw layer,
  graded against the ground truth of what was planted that night;
- the 12 legacy reports against the dbt marts that replace them. A report
  difference is explained when a table its mart is built from (dbt lineage)
  also differs that night; one that is not is a logic difference between the
  old SQL and the new, the most important thing a parallel run can find.

Pairs are compared ``reconcile.workers`` at a time, each with its own Postgres
connection and its own ClickHouse client, as the platform team's read-only
user. Every night is recorded in the ops store. The cutover is recommended on
the night that completes ``reconcile.clean_nights_for_cutover`` consecutive
nights with every table and every report within the thresholds.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from platform_ops.common.config import DISCREPANCY_CLASSES, Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.servers import clickhouse_client, connect_postgres
from platform_ops.common.store import open_store, replace_ops_table
from platform_ops.common.tagging import Tag
from platform_ops.reconcile.canonical import Rule, rules_for
from platform_ops.reconcile.classify import classify
from platform_ops.reconcile.connectors import ClickHouseConnector, PostgresConnector
from platform_ops.reconcile.diff import TableDiff, diff_table
from platform_ops.reconcile.metrics import Discrepancy, Grade, TableVerdict, grade, verdict
from platform_ops.reconcile.schema import TableSpec
from platform_ops.reconcile.specs import load_specs
from platform_ops.simulation.company import Night

NIGHTS_DDL = """
    night integer NOT NULL, run_at timestamp NOT NULL, job text NOT NULL,
    backfilled boolean NOT NULL, planted text NOT NULL, repaired text NOT NULL,
    level text NOT NULL, name text NOT NULL,
    source_rows bigint NOT NULL, target_rows bigint NOT NULL,
    missing bigint NOT NULL, extra bigint NOT NULL, rows_with_diffs bigint NOT NULL,
    rounding bigint NOT NULL, timezone_shift bigint NOT NULL, whitespace bigint NOT NULL,
    case_only bigint NOT NULL, value_mismatch bigint NOT NULL,
    row_match_rate double precision NOT NULL, passed boolean NOT NULL, failures text NOT NULL,
    rows_moved bigint NOT NULL, rows_moved_naive bigint NOT NULL,
    differing_upstream text NOT NULL, explained boolean"""
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


def discrepancies_of(spec: TableSpec, diff: TableDiff, tolerance: Decimal) -> list[Discrepancy]:
    found = [Discrepancy(spec.name, k, "", "missing_in_target", "", "") for k in diff.missing]
    found += [Discrepancy(spec.name, k, "", "extra_in_target", "", "") for k in diff.extra]
    found += [
        Discrepancy(
            spec.name,
            c.key,
            c.column,
            classify(spec.column(c.column), c.source, c.target, tolerance),
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
        self.rules: dict[str, dict[str, Rule]] = {}
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

    def _load_specs(self) -> None:
        manifest = invocation_from_settings(self.settings).manifest_path
        with connect_postgres(self.settings) as connection:
            client = clickhouse_client(self.settings, "platform")
            try:
                self.specs = load_specs(
                    self.settings, connection, client, manifest, self.config_path
                )
            finally:
                client.close()
        self.rules = {s.name: rules_for(s, self.settings.reconcile) for s in self.specs}

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
        found = discrepancies_of(spec, diff, self.tolerance)
        return PairResult(
            spec, diff, found, verdict(spec, diff, found, self.settings.reconcile.thresholds)
        )

    def __call__(self, night: Night) -> None:
        self._night_at = night.run_at
        if not self.specs:
            # On the first night, not before: nothing is touched until the
            # company's run has checked its passwords and built the marts.
            self._recreate_store()
            self._load_specs()
        # The largest pairs first, so the pool is not left waiting on one at the end.
        with ThreadPoolExecutor(self.settings.reconcile.workers) as pool:
            pairs = list(pool.map(self._compare, self.specs))
        tables = [p for p in pairs if p.spec.level == "table"]
        found = [d for p in tables for d in p.discrepancies]
        specs = {p.spec.name: p.spec for p in tables}
        result = NightResult(
            night, pairs, grade(list(night.truth), found, specs, {n: self.rules[n] for n in specs})
        )
        differing = {p.spec.name for p in tables if not p.diff.clean}
        for pair in pairs:
            report_differs = pair.spec.level == "report" and not pair.diff.clean
            if report_differs and not differing.intersection(pair.spec.upstream):
                result.unexplained.append(pair.spec.name)
        self.results.append(result)
        self._record(result, differing)

    def _record(self, result: NightResult, differing: set[str]) -> None:
        n = result.night
        job = "delivered" if n.delivered else "fixed"
        nights, examples = [], []
        limit = self.settings.reconcile.examples_per_class
        for pair in result.pairs:
            d, v = pair.diff, pair.verdict
            upstream = sorted(differing.intersection(pair.spec.upstream))
            explained = None
            if pair.spec.level == "report":
                explained = d.clean or bool(upstream)
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
                    ", ".join(upstream),
                    explained,
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
