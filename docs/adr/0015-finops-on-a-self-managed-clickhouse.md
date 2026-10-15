# 0015: FinOps on a self-managed ClickHouse: the stated bill shared out by measured reads

Status: accepted
Date: 2026-10-13

## Context

In 1.x the cost module estimated what each query would have cost on BigQuery or Snowflake: bytes from logical column sizes, compute time from a model, and a simulated workload replayed on DuckDB. Version 2.0's company runs a self-managed ClickHouse, which sends no bill per query. The company pays for machines and disks by the month, and the questions a platform team gets asked change with that: which team uses how much of the cluster, what the migration cost on top of the legacy system, when it pays back, and which habits to change.

ClickHouse answers part of this itself. Its query log records every statement with the bytes and rows it read, the tables it touched and who ran it, and `system.parts` records every table's size. What it does not record is which team, dashboard or dbt model a query was for, and its numbers had to be shown to repeat between two runs before they could carry a bill.

Three choices were the owner's: the raw layer is merged (`OPTIMIZE FINAL`) every night, the prices are public list prices with their sources, and quotas are suggested in the report, never applied.

## Decision

- **Every query the company runs is tagged** through ClickHouse's `log_comment` setting with a small JSON tag: the run, the simulated time, the kind of work (dbt, ingestion, maintenance, reconcile, dashboard, ad hoc), its subject (the dbt node, raw source, exposure or reconciled pair) and, for ad hoc SQL, the person. dbt carries it through its profile's `custom_settings`, the others per query. The cost module reads back only its own run's tagged queries, numbered in a stable order, so two runs give the same ids for the same work.
- **Measured, not estimated.** Bytes and rows are ClickHouse's own `read_bytes` and `read_rows`. Bytes written are not used: how ClickHouse cuts blocks varies between runs. Statements that only change the catalogue, or only read system tables, are collected and shown but not billed, as no warehouse bills them.
- **The nightly merge makes reads repeat.** Reads with `FINAL` on a `ReplacingMergeTree` depend on which parts the background merges have reached, which no one controls. After each day's load the raw layer is merged with `OPTIMIZE TABLE ... FINAL` (3.6 seconds for all nine tables at scale 1.0), recorded as the platform team's maintenance work, and every later `FINAL` read sees one part. dbt writes one part per partition (one insert thread, the whole insert squashed into one block): with ClickHouse's default of one insert thread per core, a model of two million rows landed in 20 to 29 parts in a different way on every run, and the same filtered query read 262,491 rows in one run and 246,198 in the next. Workload queries run on one thread, so a `LIMIT` stops after the same rows every time.
- **The cluster's bill is stated, then shared out.** `cost.clickhouse` holds the instance and disk at public on-demand list prices (us-east-1, sources next to them in `config/settings.yaml`), sized for this company: one `m6i.2xlarge` node with 500 GB of gp3, $320.32 a month. Each month of the window gets its prorated share. Compute is shared by the bytes each query read, storage by each table's uncompressed bytes in `system.parts` at the month's end. Shares are exact to 1e-12 dollars and the remainder goes to the largest, so they add up to the stated cost exactly.
- **The same workload priced two other ways.** BigQuery on demand (bytes read, 10 MB minimum per table) and Snowflake warehouses (the burst model of ADR 0004) are priced on the measured bytes, so the report can say what the same company would pay elsewhere.
- **Who pays (ADR 0004, extended).** Production goes to the owner of what was built or loaded: dbt to the model's owner, ingestion and the nightly merge to the source's owner. Consumption goes to the reader: a dashboard refresh to the dashboard's owner (the `metabase` user only runs it), ad hoc SQL to the person's team. The parallel run's reconciliation is migration cost, carried by the platform team and shown on its own.
- **The migration's business case.** The legacy Postgres stays as the operational database; what the migration retires is its reporting replica (`cost.legacy_reporting`: one `db.m6i.2xlarge` RDS instance with 500 GB, $577.50 a month). The replica runs until the cutover the reconciliation recommends, ClickHouse for the whole window, and the report shows both by month, the overlap and the months the saving takes to pay it back.
- **What to change, as suggestions.** Unused tables by lineage (ADR 0004's rule, on the query log), full-scan hotspots from ClickHouse's own rows read against each table's rows, incremental candidates checked against the sources the legacy database changes in place (`ops.source_changes`), and per-user quotas at twice the 95th-percentile day and twice the largest query, as `CREATE QUOTA` and `ALTER USER` statements. Users that also build, load or reconcile get none.

## Consequences

Every dollar on the report traces to a stated price with a source, a byte ClickHouse measured and a tag the company's own code set. The bill reconciles to the invoice, which is what lets a platform team hand it to finance.

The 1.x estimate is gone with its biases: no logical sizes, no snapshots up to four weeks old, no proxy accuracy file. In their place are two dependencies the toolkit now has on ClickHouse's behaviour, both checked by tests: that `read_bytes` repeats once the raw layer is merged, and that `log_comment` reaches the query log for every statement dbt sends.

The nightly merge has a price, and the report shows it: at scale 1.0 it rewrote 35.12 GB over the 91 days and took $209.44 of the cluster's $851.51 compute, a quarter. That is the cost of reads that repeat, and of deduplicating with `FINAL`. A real team would weigh it against merging less often or deduplicating in staging with `argMax`; here it stays, because a cost report that changes between two identical runs could not be signed off.

The sizing is an assumption, stated first in the report. A self-managed cluster's real cost also includes the people who run it, which no list price covers; the business case compares infrastructure only, and says so.

## Later note

Version 2.0, Phase 14: the window grew to 112 days with three production weeks ([ADR 0016](0016-incidents-on-the-companys-production-runs.md)), so the figures above are the 91-day ones of Phase 13; the cost README has the current ones (the merge is now about a fifth of the compute). The snapshot tables are merged before every snapshot and build, for the same reason as raw: a pair of clean runs differed by 80 bytes read in one snapshot upsert until they were.
