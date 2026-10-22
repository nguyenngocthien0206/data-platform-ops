# reconcile

Before a legacy system is switched off, someone who owns the data has to sign that the new one holds what the old one held, and keeps holding it. In version 2.0 the company migrates its reporting from a legacy Postgres to ClickHouse, and the last six weeks of its window are the **parallel run**: both systems run, dbt builds every night, and every night this module compares them. The result is `reports/reconciliation.md`, a sign-off that states its acceptance criteria before any number, then the night the cutover can be recommended, and why ([ADR 0014](../../../docs/adr/0014-the-parallel-run-and-the-cutover-rule.md)).

## How it fits together

| Step | Module | Output |
|---|---|---|
| Declare | `config/reconcile.yaml`, `schema.py`, `specs.py`: the 9 operational tables and the 12 reports to compare, their keys; columns and types read from both catalogues | |
| Break | `simulation/migration.py`, `simulation/ingest.py`: the delivered job's four defects, then seven one-off faults planted and repaired | ground truth per night |
| Render | `canonical.py`: one canonical string per value and one row hash, identical in Postgres, ClickHouse and Python | |
| Connect | `connectors.py`: Postgres hashes each row once into a temp table; ClickHouse renders on the fly, as the platform team's read-only user | |
| Diff | `diff.py`: segment summaries level by level over the MD5 of the key, rows fetched only for differing leaves | |
| Classify | `classify.py`: missing, extra, rounding, time zone shift, whitespace, case, value mismatch | |
| Judge | `metrics.py`, `parallel.py`: verdict per pair against the thresholds, grading against ground truth, report differences explained through dbt lineage, the cutover rule | `ops.reconcile_nights`, `_examples`, `_grades`, `_labels`, `_cutover` |
| Report | `report.py`, from the ops store | `reports/reconciliation.md` |

Nothing in the diff reads the ground truth. Only the grading does, the same split as the incident module.

## The decisions that shape the numbers

**The parallel run is part of the company's history.** The ingestion runs as the migration team first delivered it from the first day of the window, with four defects a real copy job ships with: payments' `created_at` read as New York time, payment amounts passed through a float and truncated on the way back, customers' emails right-trimmed, and the last key of every 25,000-key page of web sessions lost, which no dbt test notices. Nobody compares anything until the parallel run starts, seven weeks in, so that is when they are found. A week later the fixed job ships and backfills raw, and seven one-off faults follow, every other night, each repaired the next day, the way a team works through its morning report. Because it all happens in the company's own run, the backfill, the nightly builds and the reconciliation's queries are in ClickHouse's query log, which is the cost of the migration that Phase 13 puts a number on.

**Two levels, and the second one is the one the business signs.** The operational tables say whether the copy is right. The reports say whether the new dashboards show what the old reports showed. A report difference is explained when a table its mart is built from differs the same night (dbt lineage); a difference with no upstream cause is a logic difference between the legacy SQL and the dbt model, and blocks a clean night. Two were found before any defect was planted, while the comparison was being built: a ratio that ClickHouse truncated where Postgres rounded, and a return on ad spend of 0 where the legacy report said "none". The marts now follow the legacy reports, because changing a definition is a decision for after cutover, not a side effect of the migration.

**One canonical string per value, in every engine.** A checksum diff only works if two engines hash the same data the same way, and they disagree on almost everything: decimal padding, boolean spelling, time formats, `NULL` in a condition. Every value is rendered first (decimals at fixed scale, timestamps as UTC with microseconds, a NULL sentinel that never equals an empty string) and each row is hashed as MD5 of its UTF-8 bytes, summed per segment as two 32-bit halves. A golden-row test holds Python, Postgres and ClickHouse to byte-identical output (ADR 0006).

**Equality is a policy the data owner signs.** Whether trailing spaces or letter case count as differences is not a technical question. The rules live in `settings.yaml` per column, both sides use them, and the report prints them.

**Only differing segments are opened.** Rows fall into 16 segments by the first hex digit of their key's MD5, 256 by the first two, and so on. Each engine returns a count and two hash sums per segment, and only segments that disagree are split again, down to about 256 rows. An integer key, a date and a key of three columns all segment the same way.

**Every row counts, not every key.** A differing segment's rows are compared as a list, not as one row per key, so a key that appears twice on either side is reported as `duplicate_key` with how many rows each side has. Its cells are not compared, because nobody can say which copy is "the" row, and its rows count as unmatched. As a last net, a pair fails when the two sides' row counts differ by more than the missing, extra and duplicated rows explain.

**The legacy side is compared as of the copy.** The application keeps writing after the copy has read it: every night of the parallel run, the next hour of new rows reaches Postgres between the ingestion and the comparison. Each table is compared up to the copy's watermark on its `changed_at` column (`updated_at`), so a row written later makes no segment differ and costs nothing; a row changed in place later still has its old version in the copy, and counts as in flight, not as a difference. Reports are views, which Postgres cannot read as they were, so they are frozen into `reporting_asof` in one snapshot just before the copy reads, and the frozen tables are compared. A pair whose legacy side is more than `max_watermark_lag_hours` behind fails, so a copy that stops cannot hide behind its own watermark.

**The cutover rule is fixed before the run.** A night is clean when every table and every report is within the thresholds and no report difference is unexplained; the cutover is recommended on the night that completes 14 consecutive clean nights.

## Results at scale 1.0

From an actual `make docker-demo` at the default scale, the 42 nights of the parallel run (2026-02-23 to 2026-04-05), as `reports/reconciliation.md` records them.

**Verdict: cutover recommended on 2026-03-30**, the night that completed 14 consecutive clean nights. The whole demo, the parallel run and the production weeks included, took 52 minutes 30 seconds and 51 minutes 57 seconds in two clean runs of the 2.0.0 release, in the container on the development laptop. <!-- readme-check: runtime -->

**The delivered job fails on its first compared night, and every defect is found that night.** On 2026-02-23 the 9 tables gave 476,961 timezone shifts (every payment), 26,335 one-cent truncations, 349 trimmed emails and 74 lost web sessions, all found and correctly classified. Four reports failed with them, each explained by an upstream table: 28,379 orders differ in the cash reconciliation, where the shifted payment times flip late-payment flags and the truncated amounts leave paid totals a cent short, and the lost sessions take a session or two off campaign, traffic and engagement figures. The fixed job shipped on 2026-03-02 and its backfill made that night clean.

**Every one-off fault is found the night it lands.** Invented orders, deleted customers, an overwritten payment method, upper-cased emails, padded product names and shifted ticket times each failed their table on their night (the deleted customers also failed five customer-based reports, explained), and were gone the next. The six payments off by a cent were found too, but six rounding differences are within the 100 the criteria allow, so that night counted as clean: the thresholds are the data owner's statement of what matters.

| Over the whole run | |
|---|---:|
| Planted discrepancies (tables) | 3,576,911 |
| Detected | 3,576,911 |
| Recall, precision, classification accuracy | 100.000% |
| Reports differing without an upstream cause | 0 |

**The segmented diff is what makes it nightly.** On pairs that matched, it moved 11,200 rows from the engines instead of 378,885,458 for the tables (a 99.997% saving) and 14,422 instead of 39,488,792 for the reports. On pairs that differed it still saved 79.721% for the tables, and 12.578% for the reports, whose differences spread over most segments.

One night's reconciliation of all 21 pairs took 23 seconds six pairs at a time, and 102 seconds one at a time. <!-- readme-check: runtime -->

## Running it

```bash
make demo                                     # the company's window, the parallel run included
make reconcile                                # reports/reconciliation.md from the recorded nights
uv run platform-ops reconcile run --strict    # exits non-zero when the cutover is not recommended
```

Tunables live under `reconcile:` in `config/settings.yaml` (workers, leaf width, rounding tolerance, the canonical rules, the thresholds, the clean nights the cutover needs) and under `company:` (the parallel run's length, when the fix ships, the delivered job's defects). What is compared is in `config/reconcile.yaml`.

## Adopting it

The comparison engine knows nothing about the simulated company: give it two connections and a list of pairs, and it diffs Postgres against ClickHouse the way the parallel run does. What the company adds is the nightly loop around it, the planted faults, and the grading against them.

- **Runs as is, as a library:**
  - `specs.load_specs` turns `reconcile.yaml` into table specs, reading every column and type from Postgres's `information_schema` and ClickHouse's `system.columns`, and each report's upstream tables from dbt's manifest.
  - `canonical.rules_for` applies the equality policy from the settings.
  - `diff.diff_table`, with `PostgresConnector` and `ClickHouseConnector`, runs the segmented diff.
  - `parallel.discrepancies_of` classifies each difference, and `metrics.verdict` judges a pair against the thresholds.
- **Belongs to the simulated company:**
  - `ParallelRun` is called by `company run` after each simulated night and grades every night against the ground truth of what was planted (`metrics.grade`).
  - `reconcile run` writes the report from the nights it recorded.
  - The delivered job's defects and the one-off faults come from `simulation/migration.py`.
- **What you replace:**
  - **The pairs:** `reconcile.yaml` with your tables and reports, their keys, renamed columns and decimal scales, and each table's `changed_at`, the column your copy reads by.
  - **The point in time:** set each table spec's `watermark` to your copy's watermark for that table (`dataclasses.replace(spec, watermark=...)`), and pass the comparison's time to `verdict`. Freeze the legacy reports with `asof.freeze_reports` just before your copy reads, compare the frozen schema (`load_specs(..., reports_schema=...)`), and set the report specs' `watermark` to the time of the freeze.
  - **The names:** the schema and database names under `company:` in the settings (`legacy_schema`, `reporting_schema`, `raw_database`). The reports' ClickHouse database is named `marts`.
  - **The policy:** the equality rules and thresholds under `reconcile:`, which your data owner signs, not you.
  - **The loop:** a scheduled job that, every night after dbt has built, runs the pairs and stores each verdict, and your own cutover rule over the stored nights.

```python
from decimal import Decimal

from platform_ops.reconcile.canonical import rules_for
from platform_ops.reconcile.connectors import ClickHouseConnector, PostgresConnector
from platform_ops.reconcile.diff import diff_table
from platform_ops.reconcile.metrics import verdict
from platform_ops.reconcile.parallel import discrepancies_of
from platform_ops.reconcile.specs import load_specs

specs = load_specs(settings, postgres, clickhouse, manifest_path, config_path)
tolerance = Decimal(str(settings.reconcile.rounding_tolerance))
for spec in specs:
    diff = diff_table(
        spec,
        PostgresConnector(postgres),
        ClickHouseConnector(clickhouse),
        rules_for(spec, settings.reconcile),
        leaf_width=settings.reconcile.leaf_width,
    )
    found = discrepancies_of(spec, diff, tolerance)
    result = verdict(spec, diff, found, settings.reconcile.thresholds)
```

- **In order:**
  1. The operational tables first, which tell you whether the copy is right.
  2. The reports, which tell you whether the business sees the same numbers.
  3. The equality policy, agreed with the data owner before the first night.
  4. The cutover rule, written down before the run starts, so nobody negotiates it on the night.
- **Traps we hit:**
  - **Two engines rarely agree on a value's text:** decimal padding, boolean spelling, time zones, `NULL` in a condition. Everything is rendered to one canonical string first, and a golden-row test holds Python, Postgres and ClickHouse to byte-identical output (ADR 0006).
  - **ClickHouse's `avg` rounds a half to even** where Postgres rounds away from zero, and a ratio cast to a smaller scale truncates. Both showed up as report differences before any fault was planted, so compare the reports early, not only the tables.
  - **Raw in ClickHouse is read with `FINAL`,** or a row changed in place is counted twice.
  - **The legacy system never stops:** compared as it is, every row written since the copy shows as missing, every night, and no night is ever clean. Compare as of the copy's watermark, and freeze the reports before the copy reads, not after. Version 2.0.0 compared the live tables; the simulated company wrote nothing between the copy and the comparison, so it never showed.
  - **An invented row under a key the application uses next** is in flight, not extra, for one night: with only `updated_at`, a row inserted after the watermark cannot be told from one changed after it. The next copy brings the real row, and the key compares again.
  - **A report keyed on what should be unique often is not:** a join in the new model that matches two rows doubles a line of the report. Declare the key the legacy report is really unique on, and keep `duplicate_key` at 0. Version 2.0.0 kept one row per key and let such a copy pass.
  - **A report difference with no upstream cause is a logic difference** between the old SQL and the new model. It should block a clean night: it is the one a finance user finds after cutover.
  - **Thresholds are the data owner's statement of what matters:** six payments a cent off were within the rounding allowance, so that night counted as clean.
