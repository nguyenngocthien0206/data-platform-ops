# 0017: The migration is signed off by a parallel run, inside the company's own history

Status: accepted
Date: 2026-10-14

## Context

In 1.x the reconciliation proved a migration on its own synthetic legacy data, moved into Iceberg twice (the job as delivered, then fixed), in one sitting. Real migrations are not signed off that way. The old and the new system run side by side for weeks, someone compares them every night, defects are found and fixed while the business keeps working, and the cutover is approved only once the new system has stayed right for long enough. Version 2.0 has a real legacy system to compare against (the company's Postgres, its tables and its 12 hand-written reports) and a real new one (ClickHouse, the raw layer and the dbt marts), so Phase 12 could reconcile the migration that actually happens in the simulation.

Three choices were the owner's. Where the parallel run lives: inside the demo company's own timeline rather than in a separate scenario, because a separate scenario would give the same company two histories and hide what the parallel run costs from the cost phase. How long it runs: six weeks, with fourteen consecutive clean nights before cutover. And how often the reports are compared: every night, which means dbt builds every night of the parallel run.

## Decision

- **The parallel run is the last six weeks of the company's 13-week window.** The ingestion runs as the migration team first delivered it from the first day, with four defects a real copy job ships with: a UTC timestamp read as New York time, a decimal passed through a float and truncated on the way back, trailing spaces trimmed, and the last key of every page of a paged table lost. Every defect and fault touches data no dbt test checks, because what a parallel run is for is what the tests let through; the first versions paged orders and padded order statuses, and dbt's own tests stopped the build. Nobody compares anything for seven weeks, as is common, so the defects are found on the first night of the parallel run. A week later the fixed job ships and backfills raw. Seven one-off faults follow, every other night, each repaired the next day.
- **Every night, both levels.** After the ingestion and the dbt build, the 9 operational tables (Postgres `app` against ClickHouse `raw`, read with `FINAL`) and the 12 reports (Postgres `reporting` against the marts) are compared with the segmented checksum diff, six pairs at a time. What is compared is declared in `config/reconcile.yaml`; columns and types are read from both catalogues.
- **One segmentation for every key.** Segments are buckets of the MD5 of the canonical key, 16 per level, so an integer key, a date and a composite key segment the same way and a child bucket divided by 16 is its parent. ClickHouse renders and hashes byte-identically to Postgres and Python (ADR 0009, extended to ClickHouse by the golden-row test).
- **Grading and explaining.** Table-level differences are graded against the ground truth of what was planted that night, computed from the legacy rows with the defect's own rule, never from the diff. A report difference is explained when a table its mart is built from (dbt lineage) differs that night; one that is not is a logic difference between the legacy SQL and the dbt model, and a night with one is not clean.
- **The cutover rule.** A night is clean when every table and report meets the thresholds fixed before the run and no report difference is unexplained. The cutover is recommended on the night that completes 14 consecutive clean nights.
- **`make demo` runs it; `make reconcile` signs it.** The nights are recorded in the ops store as they happen; `platform-ops reconcile run` renders `reports/reconciliation.md` from them, and `--strict` fails when the cutover is not recommended.

## Consequences

The sign-off reads like one a data owner would put their name to: the thresholds first, then the recommendation with its date, then the nights that led to it. The migration team sees its defects on the first night and the one-off faults the morning after they happen, and the business owner sees that every report difference has a cause upstream, or a named logic difference to decide on before cutover.

Building the first version of the comparison already found two such differences between the legacy reports and the new marts, before any defect was planted: a ratio truncated instead of rounded, because ClickHouse divides two decimals at the dividend's scale, and a return on ad spend of 0 where the legacy report said "none". The marts now follow the legacy reports; changing a definition is a decision for after cutover, not a side effect of the migration.

It costs time. One night's reconciliation took 102 seconds at scale 1.0 one pair at a time and 23 seconds six pairs at a time, and dbt builds every night of the parallel run, so `make demo` grew from about 5 minutes to 38 (37 minutes 56 seconds in the container on the development laptop). The owner accepted that for realism; the release workflow runs the same timeline at scale 0.1, because it checks behaviour, not the numbers. The cost phase (13) now finds the parallel run in ClickHouse's query log, which is the cost of the migration it is meant to put a number on.
