# simulation

Everything that makes the simulated company behave like a real one. In version 2.0 that company runs its reporting on a legacy Postgres and is migrating to self-managed ClickHouse: the data generator feeds Postgres, hand-written reports read it, an ingestion job pulls it into ClickHouse, and dbt builds the analytics there. The workload generator and the fault injectors of 1.x live here too and come back with the modules that use them (Phases 12 to 14).

## The company's days (`company.py`)

`platform-ops company run` (and `make demo`) replays the company from scratch, always the same way:

1. **History.** The legacy Postgres is seeded with a year of data up to the window start (5 January 2026), every company database in ClickHouse is dropped, and the raw layer is backfilled from Postgres. Dropping everything matters: a dbt snapshot survives `--full-refresh`, so nothing less would start clean.
2. **Every simulated day** for 13 weeks: the day's orders, payments, sessions and tickets reach Postgres, and at 01:00 a few products and customers change in place; at 02:00 the ingestion pulls everything newer than its watermark into ClickHouse; every 28 days and on the last day, dbt builds the project (the first build in full, later ones incrementally where a model is).

The build cadence is a setting (`company.build_every_days`) because a build per day costs minutes and changes nothing the later phases measure. It is the kind of trade-off a platform team makes with the business: how fresh the dashboards need to be against how much compute that freshness burns.

## Raw data and the legacy Postgres (`raw_data.py`, `legacy_db.py`)

The generator writes nine operational tables: customers, products, orders, order items, payments, web sessions, marketing campaigns, marketing spend and support tickets. Every value comes from a hash of the seed, table, row id and column, generated with SQL inside a scratch DuckDB and inserted in primary-key order, so two runs produce the same rows ([ADR 0004](../../../docs/adr/0004-deterministic-data-generation.md)). DuckDB is only the generator's engine; `legacy_db.py` copies each table into the `app` schema of the legacy Postgres with `COPY`, with a primary key per table and an `updated_at` column that the ingestion reads.

The data is deliberately messy, because the metadata, cost and incident tooling is only worth building if it has real problems to find. Some customers register twice with the same email in a different case or with stray whitespace, so staging has to fold them before any per-customer number is right. About 5% of payments are written one to five days after they happened, which is why finance reconciliation distinguishes a missing payment from a late one. Phones, referral sources and most session user ids are nullable.

Each table is defined over a fixed id space that spans the history and the whole simulated window, and a load writes the rows that land in `(after, until]`. `HISTORY_COUNTS` sets how big the company is by the window start, so a longer window adds rows after the start rather than shrinking the history. The seed is one load up to the window start; each simulated day is another load with the same function, upserted into Postgres. A payment is never written before its order, so no load boundary can lose one. Tests check that invariant, and that many unevenly spaced loads produce exactly the same tables as a single one.

During the window, `apply_daily_changes` also updates existing rows in place: a few product prices and customer profiles each day, with a new `updated_at`. Every other table only ever gains rows. That difference is why customers and products need deduplication in ClickHouse while the event tables can be built incrementally, and it is what the customer snapshot records.

### Seed results at scale 1.0

From an actual `make seed` with the committed `config/settings.yaml`:

| Table | Rows |
|---|---:|
| customers | 48,803 |
| products | 2,000 |
| orders | 399,975 |
| order_items | 1,198,359 |
| payments | 371,450 |
| web_sessions | 1,449,825 |
| marketing_campaigns | 80 |
| marketing_spend | 10,018 |
| support_tickets | 27,355 |
| **total** | **3,507,865** |

`make seed` took 44.8 seconds, including the copy into Postgres and the backfill of the same 3,507,865 rows into ClickHouse. Of the 371,450 payments, 18,472 were written more than a day after they happened, and 806 customer accounts duplicate another one's email once case and whitespace are folded. These are the same rows 1.x generated, because the generator did not change, only where its output goes.

Row counts scale linearly with `scale_factor`, with small floors so tests at scale 0.01 still have enough rows to be meaningful.

## Legacy reports (`legacy_reports.py`, `legacy_reporting/`)

Before it had a warehouse, the company built its reports as Postgres views on the operational tables, one per dashboard: 12 reports, one for each dbt exposure, plus two helper views they share (the customer id map and an enriched order). They are plain SQL with no dbt involved, written the way an analyst would have written them years ago, and they are the numbers the business trusts today.

That makes them the reference for the migration. Each report is tested against the dbt mart that replaces it, row count and every numeric total. The first run of that test found averages a few cents off, because ClickHouse rounds a float half to even and Postgres rounds an exact value half away from zero; the marts now average the legacy way (the `avg_half_up` macro). Phase 12 turns the comparison into a nightly sign-off over a parallel run.

## Ingestion (`ingest.py`)

The company has no change data capture, so the ingestion does what many teams do at the start of a migration: every run reads the rows whose `updated_at` falls in `(watermark - overlap, now]` through ClickHouse's `postgresql()` table function, which runs on the ClickHouse server and reads Postgres directly. The overlap (60 minutes, `company.ingest_overlap_minutes`) re-reads a margin before the watermark, so a row that committed late with an older `updated_at` is not lost. A row older than the overlap is lost, and a test says so: that is the limit of a watermark, and why the overlap is a setting a team tunes against how long its longest transaction runs.

Reading a row twice is harmless. Every raw table is a `ReplacingMergeTree` versioned by `updated_at` and ordered by the primary key, so the latest version wins whether a row changed in place or was simply read again, and staging reads raw with `FINAL`. Raw tables are not partitioned, because `ReplacingMergeTree` only deduplicates within a partition and a changed row moves in `updated_at`. Every load is recorded in `ops.ingestion_loads` in the Postgres ops store, with its range and the rows it moved; the latest `loaded_until` per table is the watermark.

In the demo at scale 1.0, the ingestion moved 5,511,656 rows into ClickHouse for 5,431,300 rows written or changed in Postgres: the overlap re-read about 80,000 on purpose.

## Mart rollups (`codegen.py`)

Each business team has a handful of hand-written core facts. Dashboards want the same numbers cut by day, week and month and by one dimension or another, which is mechanical work, so `codegen.py` generates those cuts from a short declarative spec: 12 families producing 57 models, each sorted in ClickHouse by its period and dimension. The output is committed because dbt reads models from disk. Regenerate after changing the spec:

```bash
uv run python -m platform_ops.simulation.codegen
```

A test compares the committed files with a fresh render, so a hand edit to a generated model fails CI instead of drifting from the spec.

## Workload (`workload.py`), paused until Phase 13

In 1.x, `platform-ops simulation run` simulated 13 weeks of the platform on DuckDB: dbt at 02:00, the 12 dashboards refreshing every 12 or 24 hours, and five people running ad hoc SQL during office hours, from careful queries to `SELECT *` on large tables ([ADR 0007](../../../docs/adr/0007-simulated-workload-real-builds-daily-replay.md)). It is paused while the company runs on ClickHouse; Phase 13 brings it back to replay the Metabase dashboards' queries and the teams' ad hoc SQL under their own ClickHouse users, so the cost module can read who ran what from ClickHouse's query log.

From two runs of `make simulate` in 1.x, at `v1.2.0`: 91 days, 4 real dbt builds and 87 replayed, 24,934 dbt queries, 10,192 dashboard queries and 687 ad hoc queries, 5,427,803 raw rows loaded and 3,497 changed in place, in 280 and 290 seconds.

## Fault injectors (`faults.py`, `migration_faults.py`)

`faults.py` breaks the raw data on purpose for the incident scenario, and `migration_faults.py` plants labelled discrepancies in a migration for the reconciliation to find. Both still target 1.x and return with their modules: the migration faults move into the Postgres to ClickHouse load in Phase 12, and the incident faults onto real dbt runs on ClickHouse in Phase 14.
