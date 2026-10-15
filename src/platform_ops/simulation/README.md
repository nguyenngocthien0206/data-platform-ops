# simulation

Everything that makes the simulated company behave like a real one. In version 2.0 that company runs its reporting on a legacy Postgres and is migrating to self-managed ClickHouse: the data generator feeds Postgres, hand-written reports read it, an ingestion job pulls it into ClickHouse, dbt builds the analytics there, and Metabase and a few people read them, each under its own ClickHouse user. The incident faults of 1.x live here too and come back with the incident module (Phase 14).

## The company's days (`company.py`)

`platform-ops company run` (and `make demo`) replays the company from scratch, always the same way:

1. **History.** The company's ClickHouse users are created (a missing password stops the run here, before anything is written), the legacy Postgres is seeded with a year of data up to the window start (5 January 2026), every company database in ClickHouse is dropped, and the raw layer is backfilled from Postgres. Dropping everything matters: a dbt snapshot survives `--full-refresh`, so nothing less would start clean.
2. **Every simulated day** for 16 weeks: the day's orders, payments, sessions and tickets reach Postgres, and at 01:00 a few products and customers change in place (recorded in `ops.source_changes`, which the cost module's incremental check reads); at 02:00 the ingestion pulls everything newer than its watermark into ClickHouse, and the raw layer is merged (`OPTIMIZE TABLE ... FINAL`, recorded in `ops.raw_maintenance`); every 28 days and on the last day, dbt builds the project (the first build in full, later ones incrementally where a model is); then the dashboards refresh and people run their ad hoc SQL (below). On the last day of each month and of the window, every table's size is recorded for the cost module (`ops.storage_snapshots`).
3. **Weeks 8 to 13 are the migration's parallel run** (Phase 12): dbt builds every night and the migration is reconciled every night, at both levels, ending in the cutover decision (`reconcile/README.md`). The ingestion runs as the migration team first delivered it until a week into the parallel run, when the fixed job ships and backfills raw; one-off faults follow and are repaired the next day (below).
4. **The last three weeks are production** (Phase 14, `company.production_weeks`): ClickHouse is what the business runs on. Every night runs `dbt source freshness`, `dbt run`, `dbt snapshot` and `dbt test` as separate invocations, the way a production schedule does, and hands each one's artifacts to the incident desk (`incidents/README.md`). Faults happen in operation (below), and the desk tells the company when each owner's fix lands.

Outside the parallel run the build cadence is a setting (`company.build_every_days`), because a build per day costs minutes and changes nothing the later phases measure. It is the kind of trade-off a platform team makes with the business: how fresh the dashboards need to be against how much compute that freshness burns. The parallel run is the exception, because the reports are compared every night.

## Raw data and the legacy Postgres (`raw_data.py`, `legacy_db.py`)

The generator writes nine operational tables: customers, products, orders, order items, payments, web sessions, marketing campaigns, marketing spend and support tickets. Every value comes from a hash of the seed, table, row id and column, generated with SQL inside a scratch DuckDB and inserted in primary-key order, so two runs produce the same rows ([ADR 0004](../../../docs/adr/0004-deterministic-data-generation.md)). DuckDB is only the generator's engine; `legacy_db.py` copies each table into the `app` schema of the legacy Postgres with `COPY`, with a primary key per table and an `updated_at` column that the ingestion reads.

The data is deliberately messy, because the metadata, cost and incident tooling is only worth building if it has real problems to find. Some customers register twice with the same email in a different case or with stray whitespace, so staging has to fold them before any per-customer number is right. About 5% of payments are written one to five days after they happened, which is why finance reconciliation distinguishes a missing payment from a late one. Phones, referral sources and most session user ids are nullable.

Each table is defined over a fixed id space that spans the history and the whole simulated window, and a load writes the rows that land in `(after, until]`. `HISTORY_COUNTS` sets how big the company is by the window start, so a longer window adds rows after the start rather than shrinking the history. The seed is one load up to the window start; each simulated day is another load with the same function, upserted into Postgres. A payment is never written before its order, so no load boundary can lose one. Tests check that invariant, and that many unevenly spaced loads produce exactly the same tables as a single one.

During the window, `apply_daily_changes` also updates existing rows in place: a few product prices and customer profiles each day, with a new `updated_at`. Every other table only ever gains rows. That difference is why customers and products need deduplication in ClickHouse while the event tables can be built incrementally, and it is what the customer snapshot records.

### Seed results at scale 1.0

From an actual `make seed` with the committed `config/settings.yaml`:

| Table | Rows |
|---|---:|
| customers | 48,798 |
| products | 2,000 |
| orders | 399,975 |
| order_items | 1,198,359 |
| payments | 371,450 |
| web_sessions | 1,449,825 |
| marketing_campaigns | 80 |
| marketing_spend | 9,962 |
| support_tickets | 27,355 |
| **total** | **3,507,804** |

The seed took 34.6 seconds, including the copy into Postgres and the backfill of the same 3,507,804 rows into ClickHouse. Of the 371,450 payments, 18,472 were written more than a day after they happened, and 801 customer accounts duplicate another one's email once case and whitespace are folded. The id space is fitted to the window and rounded, so the 16-week window of Phase 14 left 61 fewer rows of history than the 13-week one (5 customers and 56 rows of ad spend); everything else is the same rows 1.x generated, because the generator did not change, only where its output goes.

Row counts scale linearly with `scale_factor`, with small floors so tests at scale 0.01 still have enough rows to be meaningful.

## Legacy reports (`legacy_reports.py`, `legacy_reporting/`)

Before it had a warehouse, the company built its reports as Postgres views on the operational tables, one per dashboard: 12 reports, one for each dbt exposure, plus two helper views they share (the customer id map and an enriched order). They are plain SQL with no dbt involved, written the way an analyst would have written them years ago, and they are the numbers the business trusts today.

That makes them the reference for the migration. Each report is tested against the dbt mart that replaces it, row count and every numeric total. The first run of that test found averages a few cents off, because ClickHouse rounds a float half to even and Postgres rounds an exact value half away from zero; the marts now average the legacy way (the `avg_half_up` macro). Phase 12 turns the comparison into a nightly sign-off over a parallel run.

## Ingestion (`ingest.py`)

The company has no change data capture, so the ingestion does what many teams do at the start of a migration: every run reads the rows whose `updated_at` falls in `(watermark - overlap, now]` through ClickHouse's `postgresql()` table function, which runs on the ClickHouse server and reads Postgres directly. The overlap (60 minutes, `company.ingest_overlap_minutes`) re-reads a margin before the watermark, so a row that committed late with an older `updated_at` is not lost. A row older than the overlap is lost, and a test says so: that is the limit of a watermark, and why the overlap is a setting a team tunes against how long its longest transaction runs.

Reading a row twice is harmless. Every raw table is a `ReplacingMergeTree` versioned by `updated_at` and ordered by the primary key, so the latest version wins whether a row changed in place or was simply read again, and staging reads raw with `FINAL`. Raw tables are not partitioned, because `ReplacingMergeTree` only deduplicates within a partition and a changed row moves in `updated_at`. Every load is recorded in `ops.ingestion_loads` in the Postgres ops store, with its range and the rows it moved; the latest `loaded_until` per table is the watermark.

In the demo at scale 1.0, the ingestion moved 15,521,421 rows into ClickHouse for 5,939,236 rows written or changed in Postgres: the fixed job's backfill reloads every table once, each production fix resyncs its table in full, and the overlap re-reads a margin on purpose.

## Users (`access.py`)

Nobody queries ClickHouse as an admin. There is one user per team in `config/teams.yaml`, plus `metabase`, `dbt` and `loader`, all created again at the start of every run with their grants ([ADR 0016](../../../docs/adr/0016-users-per-team-and-bi-from-exposures.md)):

| User | May | Profile |
|---|---|---|
| `sales`, `marketing`, `finance`, `product` | read the marts and the reference data | reader |
| `platform` | read every layer, raw included | reader |
| `metabase` | read the marts and the reference data, and its own entries in `system.processes` | reader |
| `dbt` | read raw, own the databases it builds, read `system.parts` | writer |
| `loader` | insert into raw, read Postgres through `postgresql()` | writer |

The `reader` profile is `readonly = 2`: no writes, though a session may still change settings. Both profiles carry the session settings the dbt profile uses, so a query means the same whoever runs it. The extra grants were found by running each user with nothing more and reading ClickHouse's refusal: dbt-clickhouse's `insert_overwrite` reads `system.parts`, and Metabase's driver looks its own queries up in `system.processes`, which a row policy keeps from showing anyone else's.

Each password comes from `CLICKHOUSE_PASSWORD_<USER>` in `.env`, with no default. That is deliberate: a shared fallback password is how test accounts end up in production, and an error that names the missing variable costs one line in `.env`.

## Dashboards (`bi.py`, `metabase.py`)

`bi.py` turns each dbt exposure into a dashboard definition: one dashboard per exposure in its owning team's collection (from the ownership registry), one native SQL card per model it reads. A card's SQL follows a rule from the model's columns: a generated rollup is a line per period and dimension value (a daily one over its last 91 days), anything else is a table sorted by its key, and a `dashboard` entry in the model's `config.meta` overrides both. Three marts carry one: daily sales, monthly revenue and the acquisition funnel.

`metabase.py` makes a running Metabase match those definitions (`make metabase`): it creates the admin account on first start, connects ClickHouse as the `metabase` user, creates a collection per team, and creates or updates every card and dashboard by name, archiving anything no exposure asks for any more. `platform-ops metabase check` runs every card through Metabase's API, and `make docker-metabase-check` adds a headless browser that opens every dashboard and saves screenshots to `reports/screenshots/metabase/`.

From an actual run on the demo at scale 1.0: 12 dashboards and 69 cards in four team collections (the platform team owns no exposure). Metabase took 68 seconds to become healthy, provisioning took 21 seconds, and the check ran all 69 cards without an error. The browser check is what found that a year of daily orders by country, about 4,600 rows, was cut off at Metabase's 2,000-row chart limit; daily charts now cover their last 91 days, and the check fails any chart that reaches the limit.

## Mart rollups (`codegen.py`)

Each business team has a handful of hand-written core facts. Dashboards want the same numbers cut by day, week and month and by one dimension or another, which is mechanical work, so `codegen.py` generates those cuts from a short declarative spec: 12 families producing 57 models, each sorted in ClickHouse by its period and dimension. The output is committed because dbt reads models from disk. Regenerate after changing the spec:

```bash
uv run python -m platform_ops.simulation.codegen
```

A test compares the committed files with a fresh render, so a hand edit to a generated model fails CI instead of drifting from the spec.

## Workload (`workload.py`)

Every simulated day, after the build, the company reads its data the way people do:

- **Metabase refreshes every dashboard** on its own schedule, every 12 hours for a `high` maturity exposure and every 24 otherwise, staggered after the 02:00 run. Each refresh runs exactly the cards `bi.py` generates for that dashboard, as the `metabase` user.
- **A few people run ad hoc SQL** during office hours, as their team's user, from a careful aggregate to `SELECT *` on the largest fact. The business teams read the marts; the platform team reads every layer, raw included.

Every query carries a tag through ClickHouse's `log_comment` setting: the run, the simulated time, the kind of work, what it was about and, for ad hoc SQL, who typed it. dbt, the ingestion, the nightly merge and the reconciliation carry the same tag (`common/tagging.py`), so the cost module reads who ran what from ClickHouse's own query log ([ADR 0018](../../../docs/adr/0018-finops-on-a-self-managed-clickhouse.md)). Rows are discarded on the server (`FORMAT Null`): what a query costs is what it reads, not what a client fetches.

Two runs send the same queries and ClickHouse measures the same reads, which took two things. Everything that looks random comes from the seed through a stable hash. And the raw layer is merged every night: a `FINAL` read of a `ReplacingMergeTree` reads whatever parts the background merges have reached, so before the nightly merge the same query read a different number of bytes on each run. Workload queries also run on one thread, so a `LIMIT` stops after the same rows every time.

In the demo at scale 1.0 the 112 days hold 12,544 dashboard refreshes and 844 ad hoc queries, and 1,008 merges of raw (nine tables a night). The merges took 3.6 seconds a night for all nine tables, and rewrote 45.48 GB over the window, about a fifth of the cluster's compute in the cost report: the price of reads that repeat. The snapshot tables are merged the same way before every snapshot and build, since each night's snapshot adds a part.

## The migration's planted discrepancies (`migration.py`, `ingest.py`)

What the parallel run has to find, with its ground truth:

- **The delivered job's four defects**, applied by the ingestion itself while it runs as delivered (`company.delivered_job`): payments' `created_at` read as New York time, payment amounts passed through a float and truncated on the way back, customers' emails right-trimmed, and the last key of every 25,000-key page of web sessions lost. A missing session breaks no dbt test (nothing references a session), so only the reconciliation can find it. The first full run paged orders instead, and dbt's relationship tests caught the orphaned order lines and payments at once, which is the right outcome for that defect but not the one a parallel run is for; order lines could not be used either, because their keys (`order_id * 10 + line`) never fall on a page boundary. Their truth is computed from the legacy rows with the defect's own rule. The float one had to be measured, not assumed: ClickHouse scales the float by 100 in floating point and truncates, which a first model (truncating the float's exact value) got wrong on 245,308 of 575,208 payments; the rule in `migration.py` now matches ClickHouse on all of them.
- **Seven one-off faults** (`CATALOGUE`), one every other night after the fix: invented orders, deleted customers, an overwritten payment method, upper-cased emails, padded product names, amounts off by a cent, shifted ticket times. Each touches a column no dbt test checks, because what a parallel run is for is what the tests let through: the first version padded order statuses, and dbt's `accepted_values` test stopped the build before the reconciliation saw it. Each picks rows from the history by a stable hash of the seed, is planted after the night's ingestion and before its build (so the reports see it too), and is repaired the next day: invented rows deleted, the others reloaded from Postgres.

## Faults in operation (`faults.py`)

During the production weeks things go wrong the way they do in production. Eight faults, two a day on four days (`incidents.fault_days`), each aimed at a check the project already has on the staging model reading the table:

| Fault | Table | What happens | What catches it |
|---|---|---|---|
| F1 null spike | orders | one row in 50 gets a NULL `customer_id` | `not_null` |
| F2 invalid category | support_tickets | one row in 50 gets priority `p0` | `accepted_values` |
| F3 orphan keys | payments | copies of one payment in 50 point at orders that do not exist | `relationships` |
| F4 stale source | web_sessions | the loader stops reading the table | freshness error |
| F5 schema drop | marketing_spend | the `clicks` column disappears; the loader cannot write the table | model error |
| F6 volume drop | orders | six rows in ten disappear | `row_count_in_range` |
| F7 schema rename | marketing_campaigns | `channel` is renamed; the loader cannot write the table | model error |
| F8 invalid category | payments | one row in 50 gets status `chargeback` | `accepted_values` |

Each is planted in ClickHouse's raw layer, the copy, never in Postgres, the source of truth: during the working day after a night's run, rows picked by a stable hash of the seed from the history before the window, which no load re-reads. 1.x had duplicate keys instead of orphans, but a `ReplacingMergeTree` deduplicates raw by key, so they cannot happen here. A stopped loader leaves its table's watermark where it was, so the first load after the fix catches up on everything it missed.

The fix is what a team does once the owner has found the cause: the raw table is resynced from Postgres in full (`Ingestion.resync`), at the time the owner's simulated response puts it (`incidents/lifecycle.py`). The next night's run then finds the root healthy and the incident resolves. Every fault, when it happened, how many rows it touched and when it was repaired is recorded in `ops.incident_faults`, the ground truth only the grading reads.

## Adopting it

This module is the simulated company itself, so none of it runs on yours. What carries over are four patterns, each a small file to read and copy:

- **Watermark ingestion without change data capture** (`ingest.py`): ClickHouse pulls from Postgres through `postgresql()`, so the copy runs on the warehouse with no extra service; the overlap re-reads a margin for late commits; `ReplacingMergeTree` makes a double read harmless; every load records its range, and the latest one is the watermark. To copy it, replace the table list (`raw_data.TABLES` and their keys) with yours and size the overlap from your longest transaction.
- **A ClickHouse user per team and per service** (`access.py`): reader and writer profiles, grants per layer, and the two extra grants the tools turned out to need (dbt-clickhouse reads `system.parts`, Metabase's driver reads `system.processes`). It is what makes the query log say who ran what, and the cost module depends on it.
- **The BI generated from dbt exposures** (`bi.py`, `metabase.py`): one dashboard per exposure in its owning team's collection, created or updated by name, so adding a dashboard goes through the same review as the model it reads. The rule for a card's SQL and the 91-day window for daily charts are the parts to keep.
- **Legacy reports as the migration's reference** (`legacy_reporting/`): keep the old SQL as views and test each dbt mart against the report it replaces, row count and every total, long before any parallel run.

Traps we hit while building the company:

- **ClickHouse's `avg` rounds a half to even**, Postgres's away from zero, so the marts average through an `avg_half_up` macro to match the legacy reports.
- **Metabase draws only the first 2,000 rows of a chart**, without a warning, so daily charts cover their last 91 days and the check fails any chart that reaches the limit.
- **A password with a shared default** is how test accounts reach production: every company user's password comes from `.env` with no default, and a missing one stops the run naming the variable.
- **`ReplacingMergeTree` deduplicates only within a partition**, so raw is not partitioned by a column that changes, such as `updated_at`.
