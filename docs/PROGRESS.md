# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 3: Incident management. Planned and approved, not yet implemented.
Branch: `phase-3-incident-management` (off `main` at `e6cdeda`).

## Done

- Phase 2 merged (PR #4). Reference numbers at scale 1.0: `make simulate` 280 to 290 s, `make cost` 20 to 24 s, byte-identical `cost.md` across two runs, unused list exactly the 12 abandoned models.
- Phase 3 plan written; three design decisions taken with the owner (below).

## In progress

Nothing. Waiting for the go-ahead to implement Phase 3.

## Decisions made

### Phase 3 (owner, at planning)

1. **Run only what faults touch.** 3-week scenario, 8 faults covering all 6 types. dbt runs for real only on days a fault is active, selecting `source:raw.<target>+` (7% to 76% of a build depending on the source). Green days run nothing, because the baseline is all green; an e2e test proves a targeted run finds the same failures as a full one.
2. **Staging roots page the source owner**, because staging only renames and casts. Routing accuracy is reported per person and per team.
3. **`make demo` drops its redundant `seed` and `build` steps** (about 52 s); `make simulate` already does both.

Verified against dbt-core 1.12.5: `dbt build` skips everything downstream of a failed test (`Fail` is in `task/build.py` `MARK_DEPENDENT_ERRORS_STATUSES`), which would hide the alert storm. `dbt run` then `dbt test` skip only on `Error`, so the scenario uses those. Severity inputs (tier-weighted downstream consumers): customers 213, orders 189, order_items and products 160, campaigns and web_sessions 76, support_tickets 34, marketing_spend 20, payments 19.

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

Defaults in brackets; none blocks starting.

1. An incident that recurs after resolution: reopen the old one or open a new one? [new one, linked by root node]
2. Do appended pages count as alerts in the "after grouping" per-person rate? [no: one page per incident]
3. Severity thresholds (SEV1 >= 150, SEV2 >= 45): tunable in config, or fixed in the ADR? [config, calibration explained in ADR 0008]

## Next step

Implement Phase 3 on `phase-3-incident-management`, in this order: incident settings and the `make demo` change; fault injector with ground truth and repair; ingestion; grouping; severity and routing; notifier and lifecycle; the scenario and `platform-ops incidents run`; metrics and report; end-to-end and targeted-vs-full tests; ADR 0008, incidents README and results here. Check early: the `source:raw.<table>+` selector with `dbt run` and `dbt test`, one root per fault on a real run, and runtime against the 2.5-minute budget.
