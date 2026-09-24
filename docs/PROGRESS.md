# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-24

## Current phase

Phase 4: Migration reconciliation. Implemented, awaiting review.
Branch: `phase-4-migration-reconciliation` (off `main` at `efacb72`).

## Done

- Phase 2 merged (PR #4). Reference numbers at scale 1.0: `make simulate` 280 to 290 s, `make cost` 20 to 24 s, byte-identical `cost.md` across two runs, unused list exactly the 12 abandoned models.
- Phase 3 implemented: fault injector with repair (`simulation/faults.py`), and `incidents/` with ingestion, grouping, severity, routing, notifier, lifecycle, the 21-day scenario, metrics and report. `platform-ops incidents run` is live; ADR 0008 and `src/platform_ops/incidents/README.md` written.
- Phase 3 reference numbers at scale 1.0: `make incidents` 2 min 22 s to 2 min 56 s over three runs, byte-identical `incidents.md` and postmortems across two runs. 8 faults, 18 failing checks, 8 incidents (exactly one per fault), 8 pages. Routing right person 8 of 8 (naive rule 3 of 8). dbt ran on 8 of 21 nights, 19 invocations.

- Phase 3 merged (PR #5).
- Phase 4 implemented: `reconcile/` (schema, canonical rules and renderers, connectors for Postgres, SQL Server and DuckDB, legacy generator, migration job with four defects, segmented diff, classification, metrics, sign-off report) and `simulation/migration_faults.py`. `platform-ops reconcile run` is live, so every Makefile target is implemented. ADR 0009 and `src/platform_ops/reconcile/README.md` written.
- Phase 4 reference numbers at scale 1.0: `make reconcile` about 52 s, byte-identical `reconciliation.md` across two runs. As delivered: 199,465 planted discrepancies, 100% recall, precision and classification accuracy; not signed off. Job fixed: 44 planted, all found; the diff moves 0.6% to 9.6% of the rows a naive comparison would. `make demo` from a clean state: 6 min 54 s.

## In progress

Nothing. Phase 4 is waiting for review.

## Decisions made

### Phase 4 (owner)

1. **PyIceberg plans the files, DuckDB reads them with `read_parquet`.** Offline; the DuckDB `iceberg` extension would need a download.
2. **`reconcile run` exits 0 whatever the verdict**; the verdict is in the report. `--strict` exits non-zero when the delivered migration is not signed off.
3. **`pymssql` as an optional extra** (`uv sync --extra sqlserver`). It carries its own driver; pyodbc needs ODBC Driver 18 on the host.
4. **Realistic time zone defect kept**: daylight saving ignored, about 65% of payments shifted by one hour.
5. **Canonical defaults**: Postgres neither trims nor folds case; SQL Server folds case, like its case-insensitive collation.
6. **Two passes** (asked during implementation): the job as delivered, then the job with its defects fixed. Systematic defects make every segment differ, so only the second pass shows what the segmented diff saves.

### Phase 4, made during implementation

24. **Legacy data has its own generator covering calendar 2025.** The Phase 1 data spans only a winter, so a daylight saving defect would never fire. Local times fall between 06:00 and midnight, avoiding the ambiguous hour when clocks go back.
25. **Hash: MD5 of the UTF-8 row string, split into two unsigned 32-bit halves, summed per segment.** Verified identical in Python, DuckDB 1.5.5, Postgres 16.15 and SQL Server 2022 CU27 on golden rows. SQL Server's legacy database uses a `_UTF8` collation so VARCHAR bytes match; style 126 drops a zero fraction, so microseconds are formatted by hand; pymssql mangles non-ASCII VARCHAR, so leaf values are fetched as NVARCHAR.
26. **Each side hashes its rows once into a temp table**, and every level groups that. The diff went from 53 s to 20 s at scale 1.0.
27. **The legacy export is read once for both passes.**
28. **Report percentages use three decimals and never round up to 100%.**
29. **Isolated test configs copy `.env`**, so the Postgres and SQL Server tests find their credentials; they skip when the engine is not reachable, so CI stays green without Docker.

### Phase 3 (owner, at planning)

1. **Run only what faults touch.** 3-week scenario, 8 faults covering all 6 types. dbt runs for real only on days a fault is active. Green days run nothing, because the baseline is all green; an e2e test proves a targeted run finds the same failures as a full one. (Selector changed during implementation, see 20.)
2. **Staging roots page the source owner**, because staging only renames and casts. Routing accuracy is reported per person and per team.
3. **`make demo` drops its redundant `seed` and `build` steps** (about 52 s); `make simulate` already does both.

Verified against dbt-core 1.12.5: `dbt build` skips everything downstream of a failed test (`Fail` is in `task/build.py` `MARK_DEPENDENT_ERRORS_STATUSES`), which would hide the alert storm. `dbt run` then `dbt test` skip only on `Error`, so the scenario uses those. Severity inputs (tier-weighted downstream consumers): customers 213, orders 189, order_items and products 160, campaigns and web_sessions 76, support_tickets 34, marketing_spend 20, payments 19.

### Phase 3, made during implementation

17. **A test that reads two models is its own subject.** A relationships test is attached to one model but depends on two. Treating it as its own node below both keeps a volume drop in orders from opening a second incident on order items.
18. **Open questions resolved with the planned defaults**: a recurrence after resolution opens a new incident linked to the old by root; appends send an update, not a page; severity thresholds stay in config.
19. **Repairs regenerate data from the seed**, no backup tables. The generator is deterministic, so "put back what the generator says" is exact; a round-trip test per fault proves every table matches a clean load.
20. **Selection is `@source:raw.<table>`, not `source:raw.<table>+`.** Every model is a table, so downstream-only runs compared fresh payments with a stale `stg_orders` and opened incidents nobody caused. `@` also rebuilds the parents of everything selected. Targeted equals full on all four fault nights.
21. **One parse, reused by every dbt invocation**, including freshness: dbt resolves `simulated_now` at run time, so freshness costs 0.6 s instead of 5.2 s.
22. **Faults land in pairs** on days 2, 5, 9 and 14, on different tables. With the freshness change this took `make incidents` from 3 min 13 s to 2 min 22 to 2 min 56 s.
23. **`dbt run` and `dbt test` are separate invocations with console logging off** in the scenario. Failing tests are expected; dbt's log file keeps the detail.

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
- `make incidents` at scale 1.0 ranged from 2 min 22 s to 2 min 56 s on this laptop, so it sometimes goes over the 2.5-minute target from the plan. With Phase 2 at about 5.3 min, the demo has roughly 1.5 to 2 min left for Phase 4 and setup.
- The alert storm is modest: 18 failing checks for 8 faults. Most downstream tests check keys and row counts that a few bad values do not break. The volume drop is the one fault with a real cascade (5 checks).
- Grouping is per run. Two unrelated faults failing the same downstream model in one run attach it to one of them by tie-break (ADR 0008).

## Open questions for the owner

None open.

## Next step

Owner reviews Phase 4 and opens the PR. Then plan Phase 5 (dashboards, root README, CI, cloud cost collectors) on a new branch from `main`.
