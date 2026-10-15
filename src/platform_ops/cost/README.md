# cost

Who spends what on the company's ClickHouse, what the migration costs on top of the legacy system, and which of it nobody needed to spend. `make demo` runs the company and leaves its traces in ClickHouse's own query log; `make cost` (`platform-ops cost report`) reads them back, shares the cluster's monthly bill out by team, prices the same workload on BigQuery and Snowflake, and writes `reports/cost.md`.

A self-managed cluster sends no bill per query. The company pays for machines and disks by the month, so the job here is the one a platform team actually has: split a fixed invoice fairly, show each team its own share, and point at the habits worth changing ([ADR 0015](../../../docs/adr/0015-finops-on-a-self-managed-clickhouse.md)).

## How it fits together

| Step | Module | Output |
|---|---|---|
| Tag | `common/tagging.py`: every query the company runs carries a JSON tag in ClickHouse's `log_comment`: the run, the simulated time, the kind of work, its subject and, for ad hoc SQL, the person | ClickHouse's `system.query_log` |
| Collect | `collect.py`: the run's tagged queries, their measured bytes and rows read, the tables they touched, literals redacted, numbered in a stable order | `ops.query_log`, `ops.query_filters` |
| Measure storage | `storage.py`: every table's rows and uncompressed bytes from `system.parts`, at each month's end and the window's | `ops.storage_snapshots` |
| Share the bill | `cluster.py`: the stated monthly cost prorated to the window, compute shared by bytes read, storage by table size, exact | `ops.query_costs` |
| Price elsewhere | `pricing.py`: BigQuery on demand and Snowflake warehouses on the same measured bytes | `ops.query_costs` |
| Attribute | `attribution.py`: production to the table's owner, consumption to the reader, the migration to the platform team | `ops.query_attribution`, `ops.cost_showback` |
| Business case | `business.py`: the legacy reporting replica until the cutover the reconciliation recommends, ClickHouse throughout, the overlap and its payback | in the report |
| Recommend | `recommend.py`: unused tables, full-scan hotspots, incremental candidates, quotas | in the report |
| Report | `report.py` | `reports/cost.md` |

## The decisions that shape the numbers

**Measured, not estimated.** Bytes and rows are what ClickHouse says each query read. An earlier version estimated them from column sizes, because DuckDB could not say it repeatably. ClickHouse can, with two conditions. The raw layer is merged every night (`OPTIMIZE TABLE ... FINAL`), because a `FINAL` read otherwise depends on how far the background merges have got. And workload queries run on one thread, so a `LIMIT` stops after the same rows every time. Bytes written are not used: how ClickHouse cuts blocks varies between runs.

**Only what a warehouse would bill is kept.** Statements that only change the catalogue (`CREATE`, `DROP`, `RENAME`) or only read ClickHouse's system tables are counted and dropped. dbt sends one or two more or fewer of them from run to run, and keeping them would have shifted every query id after them.

**The bill is stated, with its sources.** `cost.clickhouse` in `config/settings.yaml` is one `m6i.2xlarge` node with 500 GB of gp3 at public us-east-1 list prices, $320.32 a month; `cost.legacy_reporting` is the reporting replica it replaces, one `db.m6i.2xlarge` RDS instance with 500 GB, $577.50 a month. The sizing is an assumption and is printed first in the report. Compute is shared by bytes read, storage by uncompressed bytes at the month's end, both to 1e-12 dollars with the remainder on the largest share, so the shares add up to the stated cost exactly and can be handed to finance as they are.

**Each query is charged once** ([ADR 0004](../../../docs/adr/0004-pricing-models-and-attribution.md)). Building, testing, loading and merging a table is production cost for its owner. Reading it, through a dashboard or by hand, is consumption cost for the reader: a dashboard refresh goes to the dashboard's owner, not to the `metabase` user that runs it. The parallel run's reconciliation is migration cost, carried by the platform team and shown on its own, because that is the cost the business case has to pay back.

**Unused means unused all the way down.** A table is unused when neither it nor anything downstream of it was read by a dashboard or a person in the lookback window. dbt reading a table to build another does not count, so a staging model that feeds a live dashboard is never flagged, and one that only feeds dead tables is.

**Incremental is only suggested when it is safe.** The most expensive models still rebuilt in full are checked against their sources, and the legacy database's own in-place updates (`ops.source_changes`) rule out any model built on a table whose rows change.

**Quotas are suggestions.** For each team's user and Metabase: a daily read quota at twice its 95th-percentile day and a per-query limit at twice its largest query, as `CREATE QUOTA` and `ALTER USER` statements the platform team can apply or not. `ALTER USER ... SETTINGS` replaces a user's settings, so the statement restates the reader profile. Users that also build, load or reconcile get none.

## Results at scale 1.0

From actual runs of `make docker-demo` over the 112-day window (Phase 14 added three production weeks), which ends with `cost report`. Two clean runs wrote byte-identical `cost.md` files. `make cost` on its own takes about 13 seconds in the container. <!-- readme-check: runtime --> The run priced 34,207 queries: 17,779 from dbt builds and tests (the production nights' freshness, run, snapshot and test included), 1,018 ingestion loads, 1,008 nightly merges, 1,014 reconciliation queries, 12,544 dashboard refreshes and 844 ad hoc queries.

The cluster's stated cost over the window is $1,197.24 ($1,047.73 compute, $149.51 storage), and the shares add up to it exactly, month by month. A merge is an `OPTIMIZE`, which the query log does not measure like a read, so its bytes are the uncompressed size of the table it rewrote (`ops.raw_maintenance`).

| Team | ClickHouse compute | ClickHouse storage | ClickHouse total | BigQuery on demand | Snowflake warehouses |
|---|---:|---:|---:|---:|---:|
| platform | $535.55 | $87.61 | $623.16 | $1.49 | $40.77 |
| sales | $161.27 | $21.97 | $183.24 | $0.6986 | $91.53 |
| finance | $138.54 | $15.08 | $153.62 | $0.6475 | $100.07 |
| marketing | $122.44 | $13.95 | $136.39 | $0.6559 | $61.27 |
| product | $89.93 | $10.89 | $100.82 | $0.5891 | $70.06 |
| **total** | $1,047.73 | $149.51 | $1,197.24 | $4.08 | $363.71 |

| Workload | Queries | Read | ClickHouse compute | BigQuery on demand | Snowflake warehouses |
|---|---:|---:|---:|---:|---:|
| dbt builds and tests | 17,779 | 254.72 GB | $581.03 | $2.53 | $11.59 |
| ingestion from Postgres | 1,018 | 1.29 GB | $6.51 | $0.1259 | $2.88 |
| nightly merge of raw | 1,008 | 45.48 GB | $228.57 | $0.2830 | $6.22 |
| parallel run reconciliation | 1,014 | 27.21 GB | $79.77 | $0.1933 | $0.8248 |
| dashboards (Metabase) | 12,544 | 602.6 MB | $3.34 | $0.7477 | $206.64 |
| ad hoc SQL | 844 | 30.58 GB | $148.51 | $0.2012 | $135.56 |

What the numbers say:

- **On a fixed cluster, a few careless queries cost more than all the dashboards.** 844 ad hoc queries read 30.58 GB and took $148.51 of the cluster; 12,544 dashboard refreshes read 602.6 MB and took $3.34. On Snowflake the picture flips: the dashboards cost $206.64, because every refresh wakes a warehouse for seconds of work and five minutes of idling. Which habit to change first depends on how the warehouse is billed, and the report shows both.
- **The platform team carries more than half the bill, and most of it is honest.** It owns the shared layers dbt rebuilds every night ($623.16 of $1,197.24). Two lines in it are choices rather than necessities: the nightly merge of raw ($228.57, about a fifth of the compute), which buys repeatable reads and is the first thing to make cheaper (merge less often, or deduplicate in staging with `argMax` instead of `FINAL`), and the parallel run's reconciliation ($79.77), which ends with the parallel run.
- **The migration pays for itself in 3.5 months.** The reporting replica costs $577.50 a month, ClickHouse $320.32, a saving of $257.18. Running ClickHouse alongside the replica until the cutover on 2026-03-30 cost $909.30, paid back in 3.5 months. The comparison is infrastructure only; the people who run a self-managed cluster are not in a list price, and the report says so.
- **At this size, BigQuery on demand would cost $4.08.** The company's data is a few gigabytes, so a fixed cluster sized for growth is mostly idle. That is the cost argument for a serverless warehouse at small scale, and the one a platform lead should be ready to answer before the data grows into the cluster.
- **The 12 abandoned models are exactly the unused list** at both 30 and 90 days, together about $18 a month of the cluster.
- **Two full-scan hotspots.** `marts.finance_fct_payments_reconciliation` is filtered on `order_date` 12 times and read in full, because it is sorted by `order_id`; the report suggests `(order_date, order_id)`. `raw.orders` is filtered on `_loaded_at` 10 times by the platform team, where the sort key is also what `ReplacingMergeTree` deduplicates on, so the report suggests a skip index instead of a new key.
- **No incremental candidate.** The 15 most expensive full rebuilds all join customers or products, which the legacy database updates in place.
- **Quotas** at twice the busiest days: from 12.0 MB a day for Metabase to 700.0 MB for sales.

The incremental models that already exist show why layout matters as much as the setting (bytes read per build, from `ops.query_log`). The two mart models (`sales_fct_order_items`, `marketing_fct_sessions`) read 63% less in their last build than in their first full one (150.3 MB, then 56.0 MB for order items). The two intermediate ones read more with every build (`int_order_items_enriched`: 68.0 MB in the first build, 115.0 MB in the last), because they read staging views over raw, and raw is sorted by its key, not by date, so the incremental filter skips nothing.

## Adopting it

On a real platform the pricing, the bill split and the attribution are the parts to keep; what you write is the step that collects queries, because only it knows where your queries come from.

- **Runs as is, as a library:**
  - `pricing.py` prices any list of `QueryRecord`s on BigQuery on demand (`ScanPricing`) and on Snowflake warehouses (`ComputePricing`).
  - `cluster.py` shares a stated monthly cost out exactly: `pools` prorates it per month, `compute_shares` splits compute by bytes read, `storage_shares` storage by table size.
  - `attribution.py` (`attribute`, `Owners`) charges production to the table's owner and consumption to the reader, from a dbt manifest and an ownership registry.
  - `collectors/` reads BigQuery's `INFORMATION_SCHEMA.JOBS` and Snowflake's `ACCOUNT_USAGE.QUERY_HISTORY` into the same records, pinned by contract tests. It never connects itself: you hand it the rows.
- **Belongs to the simulated company:**
  - `cost report` (`run.py`) reads the company's run (`ops.company_run`), its nightly merge (`ops.raw_maintenance`), its in-place changes (`ops.source_changes`) and the cutover the parallel run recommended.
  - The ClickHouse collector (`collect.clickhouse_records`) keeps only queries carrying the simulation's tag, with its run id and simulated time.
  - The business case compares the company's stated legacy replica with its cluster.
- **What you replace:**
  - **Collection on ClickHouse:** read `system.query_log` (`type = 'QueryFinish'`) into `QueryRecord`s. The actor is the query's `user`, one per team and service ([ADR 0013](../../../docs/adr/0013-users-per-team-and-bi-from-exposures.md)). The dbt node is the `unique_id` in the comment dbt puts in front of every statement (`query-comment` in `dbt_project.yml`). The dashboard is the tag your BI sets in `log_comment`.
  - **The stated cost** (`cost.clickhouse` in the settings) with your own invoice.
  - **The report:** yours, over the shares and the attribution.
  - **On BigQuery or Snowflake:** a `Principals` mapping that says which service account is dbt and which is the BI tool.
- **In order:**
  1. One ClickHouse user per team and service, so the log knows who.
  2. dbt's query comment and the BI's tag, so it knows what for.
  3. The collector, on a week of history.
  4. Shares and attribution against your registry.
  5. Showback to each team before any chargeback: a team that sees its share for a month first argues less with the number.
- **Traps we hit:**
  - **`read_bytes` repeats only if raw is merged:** a `FINAL` read depends on how far ClickHouse's background merges got, so the company merges raw every night (about a fifth of its compute).
  - **A `LIMIT` reads the same rows only on one thread**, so workload queries run with `max_threads = 1`.
  - **`written_bytes` changes with how ClickHouse cuts blocks**, so only rows written are kept.
  - **Catalogue-only statements are dropped:** dbt sends a different number of `CREATE` and `DROP` statements from run to run.
  - **A dashboard refresh must land on the dashboard's owner,** not on the BI tool's user, or the bill tells every team that "metabase" is expensive.

The two cloud collectors make no network call; their contract tests (`tests/test_collectors.py`) use a few hundred recorded rows per vendor in `tests/fixtures/`, in the documented column names and types.
