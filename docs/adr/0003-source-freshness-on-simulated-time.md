# 0003: Source freshness is measured on simulated time

Status: accepted
Date: 2026-09-23

## Context

The toolkit compresses weeks of platform activity into minutes, so every event carries a simulated timestamp. The raw data ends at the simulated window start, in January 2026. dbt's source freshness check compares the newest `_loaded_at` in each source with the warehouse's current timestamp, which is the real clock. Measured that way, every source in this project is months stale on every run, and freshness alerts become permanent noise.

That matters beyond this project, because alert noise is an organisational problem before it is a technical one. An on-call engineer who sees the same freshness failure every morning learns to ignore it, and then misses the morning a source really stops loading. Phase 3 depends on the opposite: a "stale source" fault is injected on purpose, and it has to show up as an incident at the moment it becomes true in simulated time, and not before.

## Decision

The project overrides dbt's `collect_freshness` macro for DuckDB (`dbt/macros/collect_freshness.sql`). It uses the `simulated_now` var as the snapshot time instead of `current_timestamp()`. `platform-ops` passes the simulated clock's current time into every dbt invocation. There is deliberately no fallback to the wall clock: if the var is missing, compilation fails with a message saying why. Two sources with no daily load pattern have freshness switched off: the product catalogue, which is a snapshot, and campaign setup records, which can go weeks between launches. Daily rules on those two would only produce false alarms.

## Consequences

Freshness means something again. At the window start the orders source passes, and three simulated days later it errors. An integration test asserts both, so a dbt upgrade that silently stops using the override is caught. Runs stay deterministic, because nothing in a freshness result depends on when a person happened to run the command.

The cost is one project-level override of a dbt internal. It was checked against dbt-core 1.12.5, where `collect_freshness` is dispatched and `dbt-duckdb` ships no version of its own. A future dbt release could change that contract, and the test above is what would notice. Anyone running `dbt source freshness` by hand has to pass `simulated_now`, which is a small tax on directness in exchange for alerts the on-call rotation can trust.
