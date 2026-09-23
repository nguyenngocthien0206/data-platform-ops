# 0005: Bytes scanned from logical sizes, compute time from a model

Status: accepted
Date: 2026-09-23

## Context

A local DuckDB file sends no bill, so the cost module has to estimate what each query would cost on a cloud warehouse. The SPEC asked for bytes scanned to be estimated from "stored sizes (from DuckDB storage metadata)". Three things found while building Phase 1 and 2 ruled that out as written. First, DuckDB's compressed size is not stable: CI caught it encoding the same column as FSST on one run and Dictionary on the next, with identical data, so a price built on compressed bytes would change between two identical runs. Second, DuckDB 1.5.5's `pragma_storage_info` exposes no byte-size column at all. Third, the profiler's `total_bytes_read` counts disk reads, so it read 9.2 MB for one query and 0 for the same kind of query served from cache.

Compute time had the same problem in a different form. `ComputePricing` bills warehouse time, but measured wall-clock time moves on every run, and dbt's own `run_results.json` timings with it.

This is more than a determinism nicety. Showback only changes behaviour if the people reading it believe the number moved because of something they did. A team told its bill rose 3% will ask why, and "the storage engine picked a different codec" is an answer that ends the conversation about cost and starts one about whether the numbers can be trusted.

## Decision

Bytes scanned are **logical** bytes: the uncompressed size of each column, measured with SQL (fixed width by type for non-null values, actual byte length for strings) and recorded in `ops.table_sizes`. A query is billed for the full size of every column it reads, whatever its filter, which is how BigQuery on-demand pricing bills. Sizes are snapshotted as the data changes, raw tables every simulated day and models after every real dbt build, and each query is priced against the snapshot that was current when it ran, through a DuckDB `ASOF JOIN`.

Compute time is **modelled**: a fixed overhead per query plus bytes scanned divided by a warehouse throughput. Both constants live in `config/settings.yaml` (150 ms and 200 MB/s by default) and are printed in the report as declared assumptions, not vendor figures. Measured wall-clock time is still recorded in `ops.query_log`, but nothing is priced on it.

## Consequences

Two runs of `make simulate && make cost` produce byte-identical `cost.md` files, which an end-to-end test checks. Every number traces back to row content and two declared constants.

The estimate has known biases, stated rather than hidden. It ignores predicate pushdown and row-group pruning: a date-filtered query that DuckDB answered by scanning 157,249 of 403,009 rows is billed as if it read them all. It ignores compression, which BigQuery's on-demand billing also ignores but a Snowflake bill effectively reflects. How far the estimate is from what DuckDB actually scanned is measured on every run and written to `reports/cost_proxy_accuracy.md`. That file is kept out of `cost.md` because profiler counts depend on physical row order, which DuckDB does not promise to keep identical between runs. Two full-scale runs confirmed it: `cost.md` came out byte-identical, while the profiled rows scanned by ad hoc queries were 101,056,175 in one run and 101,170,305 in the other, because dbt's tables got a slightly different physical order in each build. Measured on those runs, the estimate assumed 234 million rows for the ad hoc queries that ran to completion, about 2.3 times what DuckDB actually scanned.

The bias points the right way for the behaviour the report is meant to encourage. Selecting fewer columns always lowers the estimate, exactly as it lowers a real on-demand bill. Adding a filter does not, because on an unpartitioned table a filter does not reduce what is billed. That gap is the argument for partitioning, which the full-scan hotspot recommendation makes explicitly.
