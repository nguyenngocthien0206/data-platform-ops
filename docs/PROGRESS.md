# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 2: Cost attribution. Implemented, all acceptance criteria verified, including at full scale.
Branch: `phase-2-cost-attribution` (off `main` at `6e5ea33`). Not committed yet, awaiting owner review.

## Done

Phase 2 acceptance, run at scale 1.0 over the 13-week window:

| Check | Result |
|---|---|
| `make simulate` | 91 days, 4 real builds and 87 replayed; 24,934 dbt, 10,192 dashboard and 687 ad hoc queries; 280 s and 290 s on two runs |
| `make cost` | 35,813 queries priced; scan $3.35, compute $296.02; 20 s and 24 s |
| Determinism | two full runs of `make simulate && make cost`: `cost.md` byte-identical (the separate accuracy note differed by 0.1%, as designed) |
| Unused tables | exactly the 12 abandoned models at both 30 and 90 days; nothing with a live dashboard downstream |
| SQL parsing | every model build's parsed reads match dbt lineage (118 of 118); all 156 tests parse |
| Pricing and parsing unit tests | both pricing models; CTEs, subqueries, `CREATE TABLE AS`, quoted identifiers and more |
| Phase 1 re-check | two clean `make seed && make build` runs: all 127 tables identical |
| `make seed` / `make build` | 8 s / 44 s from a clean state (3,507,865 raw rows) |

Time budget: seed, build, simulate and cost together take about 6 minutes of the 10-minute `make demo` budget. `make simulate` re-seeds and rebuilds on its first simulated day, so the separate `seed` and `build` steps in `make demo` (about 52 s) are redundant and could be dropped when Phase 5 finalises the demo.

Built:

- `simulation/workload.py`: 13-week simulated workload. Raw data arrives daily (with in-place changes to products and customers), dbt runs for real every 28 days and is replayed on the other days, 12 dashboards refresh every 12 or 24 hours, 5 people run ad hoc SQL. `platform-ops simulation run` / `make simulate`.
- `cost/`: `sql_parse` (sqlglot), `sizes` (logical column sizes), `growth` (append-only observation), `collect` (`QueryCollector`, `LoggedConnection`, dbt run reader), `estimate` (bytes and modelled time, ASOF-joined to size snapshots), `pricing` (`PricingModel`, `ScanPricing`, `ComputePricing`), `attribution`, `recommend`, `report`, `run`. `platform-ops cost report` / `make cost`.
- Outputs: `ops.query_log`, `ops.query_tables`, `ops.table_sizes`, `ops.source_growth`, `ops.query_estimates`, `ops.query_costs`, `ops.query_attribution`, `ops.cost_by_team`, `ops.cost_report_*`, `ops.cost_proxy_accuracy`; `reports/cost.md` and `reports/cost_proxy_accuracy.md`.
- Tests: SQL parsing edge cases (CTEs, subqueries, CTAS, quoted identifiers, `__dbt_tmp`, `SELECT *`, query comment), both pricing models, sizes, growth, estimate, attribution, recommendations, collection, workload; end-to-end determinism and acceptance at scale 0.01.
- ADRs 0005 (bytes and modelled time), 0006 (pricing and attribution), 0007 (simulated workload); `cost/README.md`.

## In progress

Nothing. Waiting for review.

## Decisions made

### Phase 2 (owner)

1. **Logical bytes, not stored bytes** (ADR 0005). Compressed size is not stable between identical runs.
2. **Modelled compute time**: 150 ms per query plus bytes at 200 MB/s, declared in settings and printed in the report. Wall-clock time is recorded, never priced.
3. **Real simulated dollars**, no projection factor.
4. **Products and customers change in place** during the window, so the incremental recommendation has sources to rule out.
5. **One warehouse per workload** (transform, BI, ad hoc) for compute pricing.
6. **BigQuery's 10 MB minimum per table** in scan pricing.
7. **13-week window** (not the SPEC's 6), so the 90-day unused check covers a real 90 days.
8. **Real dbt build every 28 days**, replayed daily in between. Weekly real builds measured at 8 to 9 minutes for `make simulate`, which breaks the 10-minute demo budget.

### Made during implementation

9. **History counts are independent of window length.** `raw_data.HISTORY_COUNTS` is the size by the window start; the id space is extended to cover the window. Otherwise stretching the window to 13 weeks would have shrunk the history.
10. **dbt builds no longer receive `simulated_now`.** Only source freshness uses it, and changing vars forces dbt to re-parse the whole project. With constant vars the saved parse is reused (full parse 9 s, then about 1 s).
11. **Row-count test bounds cover the whole window** (orders up to 680k at scale 1.0). The old upper bound failed mid-window as orders grew; the lower bound, which catches a volume drop, is unchanged.
12. **Payments are never loaded before their order.** Found by comparing seven daily appends with one seed at full scale: a payment stamped minutes before its order was lost when a load boundary fell between them. Tests now check the invariant and many uneven appends.
13. **Daily loads only generate ids that can land in the window**, about 0.4 s a day at scale 1.0 instead of 1.1 s.
14. **Writes batch into one transaction per build interval, and inserts use multi-row `VALUES`.** Measured on this laptop: each commit costs 0.3 to 0.9 s; for 400 rows, `executemany` 0.17 s, Arrow about 0.8 s (a fixed cost per call), multi-row `VALUES` 0.035 s.
15. **The proxy-accuracy report is a separate file.** It uses profiler row counts, which DuckDB does not guarantee to be identical between runs, so it must never affect `cost.md`.
16. **dbt threads 8** (from 4): 26.6 s against 30.0 s per `make build`.

### Earlier phases (still in force)

- Platform team owns sources, staging and intermediate; business teams own marts and exposures; most specific ownership rule wins, ties fail.
- Every model is a full-refresh table; abandoned models keep their team's default tier.
- `run_dbt` releases dbt-duckdb's cached DuckDB handle after every invocation.
- Freshness on simulated time; off for `products` and `marketing_campaigns`.
- GNU make via winget; mypy pinned `<1.20`, built from source; CI runs lint, tests and `metadata check --parse`.

## Known issues

- Replayed days price model reads on model sizes up to 4 weeks old, and dashboards read marts up to 4 weeks stale. Raw data is always current. Acceptable for quarterly cost attribution (ADR 0007).
- The estimate ignores row-group pruning, so filtered queries are overestimated: ad hoc queries by about 2.3 times at scale 1.0, measured per run in `reports/cost_proxy_accuracy.md`. The accuracy note only covers queries that returned less than one page (500 rows), because DuckDB has no final row count for a result cut short, so dashboard accuracy there is measured on small rollup tables only.
- Filters written against a CTE or subquery column outside it are not traced to the base table for hotspot detection. The workload and dbt do not write filters that way.
- `make demo` still fails by design until Phases 3 and 4 replace their placeholder commands.

## Open questions for the owner

None blocking.

## Next step

Owner reviews and merges `phase-2-cost-attribution`, and confirms CI is green on the PR. Then plan Phase 3 (incident management) on `phase-3-incident-management` off `main`. Phase 3 needs its own dbt runs; with about 6 of the 10 demo minutes already used, its multi-week scenario should reuse the replay approach from ADR 0007 rather than build for real every day.
