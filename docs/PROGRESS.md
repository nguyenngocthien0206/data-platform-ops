# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 2: Cost attribution. Planned, not yet implemented.
Branch: `phase-2-cost-attribution` (off `main` at `6e5ea33`).

## Done

- Phase 0 merged (PR #2). Phase 1 merged (PR #3), plus the flaky storage-layout test fix on `main`.
- Phase 2 plan written, four design decisions taken with the owner (below).
- Library behaviour Phase 2 depends on, verified against the installed versions (below).

Phase 1 reference numbers at scale 1.0: 3,525,042 raw rows (`make seed` about 6 s), 118 models and 156 tests (`make build` 32 to 35 s, of which dbt about 17 s), 139 owned datasets, 372 lineage edges, 104 tests in `make test`.

## In progress

Nothing. Waiting for the go-ahead to implement Phase 2.

## Decisions made

### Phase 2 (owner, at planning)

1. **Bytes scanned come from logical sizes, not stored sizes.** Each column's uncompressed size is measured with SQL after each build (fixed width by type, strings by actual length), and a query is billed for the full columns it references, the way BigQuery bills. Compressed size is not stable between identical runs (see Phase 1 known issues), so this departs from the SPEC wording on purpose; ADR 0005 will record why.
2. **dbt runs for real once per simulated week and is replayed on the other days.** Raw data is appended every simulated day. Each day's 02:00 run is recorded from the latest real build's compiled SQL, stamped with that day's simulated time and priced on that day's table sizes. 42 real builds would take about 12 minutes at scale 1.0 and break the 10-minute `make demo` budget.
3. **Compute time is modeled, not measured:** a fixed per-query overhead plus bytes scanned divided by a configured throughput. Wall-clock duration is still recorded in `ops.query_log` but never priced, because it changes on every run.
4. **Reports show real simulated dollars** at the configured public rates, next to bytes and compute-seconds. No projection factor.

### Earlier phases (still in force)

- Platform team owns sources, staging and intermediate; business teams own marts and exposures; team prefixes on marts; most specific ownership rule wins, ties fail.
- Every model is a full-refresh table. Abandoned models keep their team's default tier.
- `run_dbt` releases dbt-duckdb's cached DuckDB handle after every invocation.
- Writes to `ops` tables go through one transaction.
- Freshness on simulated time; off for `products` and `marketing_campaigns`.
- GNU make via winget; mypy pinned `<1.20`, built from source; CI runs lint, tests and `metadata check --parse`.

## Verified against installed versions (for Phase 2)

- **DuckDB 1.5.5 profiling:** `PRAGMA enable_profiling='no_output'`, `SET profiling_mode='detailed'`, then `connection.get_profiling_information(format='json')`. Useful keys: `cumulative_rows_scanned`, per operator `operator_rows_scanned`, `extra_info.Table` and `extra_info.Filters`. `profiling_coverage='ALL'` is accepted (default covers SELECT only). A date-filtered query on `sales_fct_orders` scanned 157,249 of 403,009 rows thanks to row-group pruning, which is the gap the proxy-accuracy section will show.
- **`total_bytes_read` is not usable:** it counts disk reads, so it was 9.2 MB for one query and 0 for the next served from cache.
- **Fetching a 1.2M-row `SELECT *` into Python took 8.6 s.** Workload queries fetch a first page only; the proxy still bills the full columns.
- **dbt-duckdb compiled SQL** in `target/run` is `create table "warehouse"."<schema>"."<model>__dbt_tmp" as (...)`; the parser must map `__dbt_tmp` back to the model. `run_results.json` has wall-clock `timing` and `execution_time` only; `adapter_response` is `{'_message': 'OK'}` with no row or byte counts.
- **sqlglot 30.19:** parses the dbt CTAS; `qualify` plus `traverse_scope` resolves base-table columns through CTEs and expands `SELECT *` given a schema. CTE names also appear as `exp.Table`, so extraction must be scope-based.

## Known issues

- Phase 1 PR: the CI rerun after the flaky-test fix should be confirmed green.
- Three orders reference a customer who signed up seconds after the order (generator edge). Harmless.
- `make demo` fails by design until Phases 2 to 4 land.
- Postgres may still be running from `make up`; `make down` stops it.

## Open questions for the owner

Defaults in brackets; none blocks starting.

1. Make `products` (price changes) and `customers` (profile changes) mutable during the simulation, so the incremental-candidate recommendation has non-append-only sources to reject? [yes]
2. One modeled warehouse per workload type (transform, BI, ad hoc) for compute pricing, so idle time lands on the workload that caused it? [yes]
3. Apply BigQuery's public 10 MB minimum per referenced table in `ScanPricing`? [yes]
4. Modeled throughput and per-query overhead are declared assumptions, not vendor numbers. OK to start at 200 MB/s and 150 ms, stated in the report? [yes]
5. The 90-day unused lookback is longer than the 6-week window, so it effectively means "never read in the whole window". Report it with that caveat? [yes]

## Next step

Implement Phase 2 on `phase-2-cost-attribution`, in this order:

1. Settings for cost, workload and proxy; `ops` table schemas.
2. `cost/sql_parse.py` with its unit tests (CTEs, subqueries, CTAS, quoted identifiers, `__dbt_tmp`, `SELECT *`).
3. `cost/sizes.py`: logical size snapshots into `ops.table_sizes`.
4. `cost/collect.py`: `QueryCollector` interface, `LoggedConnection`, dbt run ingestion.
5. `cost/pricing.py`: `PricingModel`, `ScanPricing`, `ComputePricing`, with unit tests.
6. `simulation/workload.py`: daily appends, weekly real builds with daily replay, dashboard refreshes, ad hoc users. `platform-ops simulation run`.
7. `cost/attribution.py` and `ops.cost_by_team`.
8. `cost/recommend.py`: unused tables, full-scan hotspots, incremental candidates.
9. `cost/report.py`: `ops.cost_report_*` and `reports/cost.md`. `platform-ops cost report`.
10. End-to-end and determinism tests; ADRs 0005 to 0007; `cost/README.md`; final update here.

Phase 2 acceptance: `make simulate && make cost` produces the report deterministically; every abandoned model appears in the unused list and no model with a live exposure downstream does; unit tests cover SQL parsing edge cases and both pricing models.
