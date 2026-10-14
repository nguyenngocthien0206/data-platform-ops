# cost

Who spends what on the company's ClickHouse, what the migration costs on top of the legacy system, and which of it nobody needed to spend. `make demo` runs the company and leaves its traces in ClickHouse's own query log; `make cost` (`platform-ops cost report`) reads them back, shares the cluster's monthly bill out by team, prices the same workload on BigQuery and Snowflake, and writes `reports/cost.md`.

A self-managed cluster sends no bill per query. The company pays for machines and disks by the month, so the job here is the one a platform team actually has: split a fixed invoice fairly, show each team its own share, and point at the habits worth changing ([ADR 0018](../../../docs/adr/0018-finops-on-a-self-managed-clickhouse.md)).

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

**Measured, not estimated.** Bytes and rows are what ClickHouse says each query read. In 1.x they were estimated from column sizes ([ADR 0005](../../../docs/adr/0005-scan-estimate-and-modelled-compute-time.md)), because DuckDB could not say it repeatably. ClickHouse can, with two conditions. The raw layer is merged every night (`OPTIMIZE TABLE ... FINAL`), because a `FINAL` read otherwise depends on how far the background merges have got. And workload queries run on one thread, so a `LIMIT` stops after the same rows every time. Bytes written are not used: how ClickHouse cuts blocks varies between runs.

**Only what a warehouse would bill is kept.** Statements that only change the catalogue (`CREATE`, `DROP`, `RENAME`) or only read ClickHouse's system tables are counted and dropped. dbt sends one or two more or fewer of them from run to run, and keeping them would have shifted every query id after them.

**The bill is stated, with its sources.** `cost.clickhouse` in `config/settings.yaml` is one `m6i.2xlarge` node with 500 GB of gp3 at public us-east-1 list prices, $320.32 a month; `cost.legacy_reporting` is the reporting replica it replaces, one `db.m6i.2xlarge` RDS instance with 500 GB, $577.50 a month. The sizing is an assumption and is printed first in the report. Compute is shared by bytes read, storage by uncompressed bytes at the month's end, both to 1e-12 dollars with the remainder on the largest share, so the shares add up to the stated cost exactly and can be handed to finance as they are.

**Each query is charged once** ([ADR 0006](../../../docs/adr/0006-pricing-models-and-attribution.md)). Building, testing, loading and merging a table is production cost for its owner. Reading it, through a dashboard or by hand, is consumption cost for the reader: a dashboard refresh goes to the dashboard's owner, not to the `metabase` user that runs it. The parallel run's reconciliation is migration cost, carried by the platform team and shown on its own, because that is the cost the business case has to pay back.

**Unused means unused all the way down.** A table is unused when neither it nor anything downstream of it was read by a dashboard or a person in the lookback window. dbt reading a table to build another does not count, so a staging model that feeds a live dashboard is never flagged, and one that only feeds dead tables is.

**Incremental is only suggested when it is safe.** The most expensive models still rebuilt in full are checked against their sources, and the legacy database's own in-place updates (`ops.source_changes`) rule out any model built on a table whose rows change.

**Quotas are suggestions.** For each team's user and Metabase: a daily read quota at twice its 95th-percentile day and a per-query limit at twice its largest query, as `CREATE QUOTA` and `ALTER USER` statements the platform team can apply or not. `ALTER USER ... SETTINGS` replaces a user's settings, so the statement restates the reader profile. Users that also build, load or reconcile get none.

## Results at scale 1.0

From actual runs of `make docker-demo` over the 91-day window, which ends with `cost report`. Two clean runs wrote byte-identical `cost.md` files. `make cost` on its own takes about 13 seconds in the container. <!-- readme-check: runtime --> The run priced 25,549 queries: 12,009 from dbt builds and tests, 828 ingestion loads, 819 nightly merges, 1,014 reconciliation queries, 10,192 dashboard refreshes and 687 ad hoc queries.

The cluster's stated cost over the window is $973.02 ($851.51 compute, $121.51 storage), and the shares add up to it exactly, month by month. A merge is an `OPTIMIZE`, which the query log does not measure like a read, so its bytes are the uncompressed size of the table it rewrote (`ops.raw_maintenance`).

| Team | ClickHouse compute | ClickHouse storage | ClickHouse total | BigQuery on demand | Snowflake warehouses |
|---|---:|---:|---:|---:|---:|
| platform | $458.63 | $71.26 | $529.89 | $1.11 | $33.44 |
| sales | $126.64 | $17.83 | $144.47 | $0.5064 | $74.23 |
| finance | $102.92 | $12.23 | $115.15 | $0.4593 | $80.39 |
| marketing | $89.74 | $11.32 | $101.06 | $0.4648 | $50.57 |
| product | $73.57 | $8.87 | $82.45 | $0.4289 | $56.63 |
| **total** | $851.51 | $121.51 | $973.02 | $2.97 | $295.25 |

| Workload | Queries | Read | ClickHouse compute | BigQuery on demand | Snowflake warehouses |
|---|---:|---:|---:|---:|---:|
| dbt builds and tests | 12,009 | 165.51 GB | $416.22 | $1.68 | $7.63 |
| ingestion from Postgres | 828 | 812.0 MB | $5.63 | $0.1013 | $2.78 |
| nightly merge of raw | 819 | 35.12 GB | $209.44 | $0.2198 | $5.97 |
| parallel run reconciliation | 1,014 | 27.21 GB | $79.59 | $0.1933 | $0.8248 |
| dashboards (Metabase) | 10,192 | 485.1 MB | $3.12 | $0.6075 | $167.90 |
| ad hoc SQL | 687 | 24.62 GB | $137.51 | $0.1623 | $110.14 |

What the numbers say:

- **On a fixed cluster, a few careless queries cost more than all the dashboards.** 687 ad hoc queries read 24.62 GB and took $137.51 of the cluster; 10,192 dashboard refreshes read 485.1 MB and took $3.12. On Snowflake the picture flips: the dashboards cost $167.90, because every refresh wakes a warehouse for seconds of work and five minutes of idling. Which habit to change first depends on how the warehouse is billed, and the report shows both.
- **The platform team carries more than half the bill, and most of it is honest.** It owns the shared layers dbt rebuilds every night ($529.89 of $973.02). Two lines in it are choices rather than necessities: the nightly merge of raw ($209.44, a quarter of the compute), which buys repeatable reads and is the first thing to make cheaper (merge less often, or deduplicate in staging with `argMax` instead of `FINAL`), and the parallel run's reconciliation ($79.59), which ends at cutover.
- **The migration pays for itself in 3.5 months.** The reporting replica costs $577.50 a month, ClickHouse $320.32, a saving of $257.18. Running ClickHouse alongside the replica until the cutover on 2026-03-30 cost $909.30, paid back in 3.5 months. The comparison is infrastructure only; the people who run a self-managed cluster are not in a list price, and the report says so.
- **At this size, BigQuery on demand would cost $2.97.** The company's data is a few gigabytes, so a fixed cluster sized for growth is mostly idle. That is the cost argument for a serverless warehouse at small scale, and the one a platform lead should be ready to answer before the data grows into the cluster.
- **The 12 abandoned models are exactly the unused list** at both 30 and 90 days, together about $16 a month of the cluster.
- **Three full-scan hotspots**, each filtered 11 times and read in full. Two marts are filtered on `order_date` but sorted by `order_id`; the report suggests `(order_date, order_id)`. The third is `raw.orders`, filtered on `_loaded_at` by the platform team, where the sort key is also what `ReplacingMergeTree` deduplicates on, so the report suggests a skip index instead of a new key.
- **No incremental candidate.** The 15 most expensive full rebuilds all join customers or products, which the legacy database updates in place.
- **Quotas** at twice the busiest days: from 12.0 MB a day for Metabase to 850.0 MB for sales.

The incremental models that already exist show why layout matters as much as the setting (bytes read per build, from `ops.query_log`). The two mart models (`sales_fct_order_items`, `marketing_fct_sessions`) read 77% less in their last build than in their first full one (150.3 MB, then 34.2 MB for order items). The two intermediate ones read more with every build (`int_order_items_enriched`: 68.0 MB in the first build, 105.2 MB in the last), because they read staging views over raw, and raw is sorted by its key, not by date, so the incremental filter skips nothing.

## On a real warehouse

Collection is the only step that knows where queries came from. On a real ClickHouse cluster the collector is the same, `clickhouse_records`, run with a read-only role after the access and data handling review; what the company has to add is the tag, which dbt can carry through its profile's `custom_settings` and a BI tool through its connection settings.

For the cloud warehouses, `cost/collectors/` has two collectors implementing the same `QueryCollector` protocol:

- `BigQueryJobsCollector` reads `INFORMATION_SCHEMA.JOBS`. It skips jobs that are not queries, not finished, failed, served from cache, and the parent job of a script (its children carry the statements), and counts each reason.
- `SnowflakeQueryHistoryCollector` reads `ACCOUNT_USAGE.QUERY_HISTORY`. It skips `fail` and `incident` statuses and keeps result-cache hits, which scan nothing but still ran.

Both take a `Principals` mapping that says which service account is dbt and which is the BI tool; everyone else is a person, recorded by the name before the `@` so it lines up with `config/teams.yaml`. The dbt node comes from the query comment dbt appends to every statement, and a dashboard from its job label or query tag.

Neither collector makes a network call. They are pinned by contract tests (`tests/test_collectors.py`) against a few hundred recorded rows per vendor in `tests/fixtures/`, written in the documented column names and types. The fixtures were generated in 1.x from the simulated query log by `scripts/make_vendor_fixtures.py`, which needs the 1.x simulation and runs from tag `v1.2.0`.
