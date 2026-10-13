# reconcile

Before a legacy system is switched off, someone who owns the data has to sign that the new one holds what the old one held, and keeps holding it. In version 2.0 the company migrates its reporting from a legacy Postgres to ClickHouse, and the last six weeks of its window are the **parallel run**: both systems run, dbt builds every night, and every night this module compares them. The result is `reports/reconciliation.md`, a sign-off that states its acceptance criteria before any number, then the night the cutover can be recommended, and why ([ADR 0017](../../../docs/adr/0017-the-parallel-run-and-the-cutover-rule.md)).

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

**One canonical string per value, in every engine.** A checksum diff only works if two engines hash the same data the same way, and they disagree on almost everything: decimal padding, boolean spelling, time formats, `NULL` in a condition. Every value is rendered first (decimals at fixed scale, timestamps as UTC with microseconds, a NULL sentinel that never equals an empty string) and each row is hashed as MD5 of its UTF-8 bytes, summed per segment as two 32-bit halves. A golden-row test holds Python, Postgres and ClickHouse to byte-identical output (ADR 0009).

**Equality is a policy the data owner signs.** Whether trailing spaces or letter case count as differences is not a technical question. The rules live in `settings.yaml` per column, both sides use them, and the report prints them.

**Only differing segments are opened.** Rows fall into 16 segments by the first hex digit of their key's MD5, 256 by the first two, and so on. Each engine returns a count and two hash sums per segment, and only segments that disagree are split again, down to about 256 rows. An integer key, a date and a key of three columns all segment the same way.

**The cutover rule is fixed before the run.** A night is clean when every table and every report is within the thresholds and no report difference is unexplained; the cutover is recommended on the night that completes 14 consecutive clean nights.

## Results at scale 1.0

From an actual `make docker-demo` at the default scale, the 42 nights of the parallel run (2026-02-23 to 2026-04-05), as `reports/reconciliation.md` records them.

**Verdict: cutover recommended on 2026-03-30**, the night that completed 14 consecutive clean nights. The whole demo, parallel run included, took 37 minutes 56 seconds in the container on the development laptop. <!-- readme-check: runtime -->

**The delivered job fails on its first compared night, and every defect is found that night.** On 2026-02-23 the 9 tables gave 476,961 timezone shifts (every payment), 26,335 one-cent truncations, 351 trimmed emails and 74 lost web sessions, all found and correctly classified. Four reports failed with them, each explained by an upstream table: 28,379 orders differ in the cash reconciliation, where the shifted payment times flip late-payment flags and the truncated amounts leave paid totals a cent short, and the lost sessions take a session or two off campaign, traffic and engagement figures. The fixed job shipped on 2026-03-02 and its backfill made that night clean.

**Every one-off fault is found the night it lands.** Invented orders, deleted customers, an overwritten payment method, upper-cased emails, padded product names and shifted ticket times each failed their table on their night (the deleted customers also failed five customer-based reports, explained), and were gone the next. The six payments off by a cent were found too, but six rounding differences are within the 100 the criteria allow, so that night counted as clean: the thresholds are the data owner's statement of what matters.

| Over the whole run | |
|---|---:|
| Planted discrepancies (tables) | 3,576,917 |
| Detected | 3,576,917 |
| Recall, precision, classification accuracy | 100.000% |
| Reports differing without an upstream cause | 0 |

**The segmented diff is what makes it nightly.** On pairs that matched, it moved 11,200 rows from the engines instead of 378,892,260 for the tables (a 99.997% saving) and 14,422 instead of 39,488,818 for the reports. On pairs that differed it still saved 79.822% for the tables, and 12.577% for the reports, whose differences spread over most segments.

One night's reconciliation of all 21 pairs took 23 seconds six pairs at a time, and 102 seconds one at a time. <!-- readme-check: runtime -->

## Running it

```bash
make demo                                     # the company's window, the parallel run included
make reconcile                                # reports/reconciliation.md from the recorded nights
uv run platform-ops reconcile run --strict    # exits non-zero when the cutover is not recommended
```

Tunables live under `reconcile:` in `config/settings.yaml` (workers, leaf width, rounding tolerance, the canonical rules, the thresholds, the clean nights the cutover needs) and under `company:` (the parallel run's length, when the fix ships, the delivered job's defects). What is compared is in `config/reconcile.yaml`.
