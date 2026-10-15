# 0012: ClickHouse modelling per layer, and ingestion by watermark

Status: accepted
Date: 2026-10-13

## Context

Version 2.0 moves the simulated company from a legacy Postgres to self-managed ClickHouse, with its analytics rebuilt as one dbt project of about 120 models (Phase 11a). The 1.x project was a nightly full refresh of every model on DuckDB, which no team would run on a real cluster: on ClickHouse the engine and the keys of a table decide what every later query costs, and Phase 13 prices exactly that. So the choices could not be left to the adapter's defaults, and they had to be written down, because the platform team that picks a sort key is not the team that pays for reading the table.

Two facts about the company shaped the choices. It has no change data capture, like many teams at the start of a migration, and nobody would stand up Debezium and Kafka on a laptop. And two of its tables change in place (customer profiles, product prices) while everything else only gains rows.

## Decision

- **Ingestion by an `updated_at` watermark**, through ClickHouse's `postgresql()` table function, which runs on the ClickHouse server and reads Postgres directly. Each load reads `(watermark - 60 minutes, now]`: the overlap re-reads a margin so a row committed late with an older `updated_at` is not lost. Every load is recorded in `ops.ingestion_loads` in the Postgres ops store, and the latest `loaded_until` per table is the watermark. The loads run as their own ClickHouse user, `loader` (ADR 0013).
- **Raw tables are `ReplacingMergeTree(updated_at)`, ordered by the primary key and not partitioned.** A row read twice by the overlap, or changed in place, keeps only its latest version. They are not partitioned because `ReplacingMergeTree` deduplicates only within a partition, and a changed row moves in `updated_at`, so a partition by time would keep both versions forever.
- **Staging is views reading raw with `FINAL`.** One row per key, the latest version, without storing the data a second time. The cost is paid on every read instead of once on write, which is acceptable while staging is read only by dbt.
- **Every other model is a `MergeTree` table sorted by its own key**, the key its `unique` test already declares. Keys may be nullable on the table folders (`allow_nullable_key`), because the legacy columns are and the keys derive from them. The abandoned models keep the unsorted default, the way untuned tables look in a real warehouse.
- **The four large event tables are incremental** (`int_order_items_enriched`, `int_sessions_enriched`, `sales_fct_order_items`, `marketing_fct_sessions`): `insert_overwrite` on monthly partitions, rebuilding the current and previous month, so a late payment or session still lands in its month. Everything else is a full-refresh table.
- **Numbers mean what they meant on Postgres.** Money stays in `Decimal` (a float discount rate made revenue sums depend on aggregation order), averages are exact and round half away from zero (`avg_half_up`), and every session runs with `join_use_nulls = 1` and `union_default_mode = 'DISTINCT'`, so SQL behaves as standard SQL.
- **A run starts over completely**: every company database is dropped, because a dbt snapshot survives `--full-refresh`. Two clean runs then leave identical data on both servers.

## Consequences

The company's runs repeat exactly (all 155 relations identical across two clean runs at scale 1.0), which is what lets later phases put a cost or a sign-off on a number and have a reviewer reproduce it.

The watermark has a stated limit instead of a hidden one: a row older than the overlap when it commits is lost, and a test shows it. That turns a vague "the pipeline sometimes misses rows" into a setting a team tunes against its longest transaction, and a known reason to move to change data capture when the volume justifies running it. The overlap is not free either: at scale 1.0 it re-read about 80,000 of 5.5 million rows.

Incremental models do not pay off at this size: an incremental build takes 30 seconds and a full refresh 26, because about 290 dbt nodes of fixed overhead dominate. What they save is bytes read, and Phase 13 measures that from ClickHouse's own query log rather than assuming it. Those measurements will also say whether a sort key chosen by the platform team serves the business teams that query the marts, which is the conversation this record exists to support.
