# 0007: The simulated workload: real builds every four weeks, replayed daily runs

Status: accepted
Date: 2026-09-23

## Context

The cost module needs months of realistic query history: a nightly dbt run, dashboards refreshing on their own schedules, and people running ad hoc SQL of mixed quality. The SPEC schedules dbt daily, and the 90-day unused-table check needs at least 90 days of history, so the window is 13 weeks (91 days).

A real dbt build of the 118-model project takes about 26 seconds at scale 1.0, and it is not a parsing cost that caching removes: once dbt's saved parse is reused, the time is spent executing 274 models and tests, and data volume barely moves it (about 17 seconds at scale 0.01). Ninety-one real builds would take about 40 minutes. Weekly builds were tried first and measured at 8 to 9 minutes for `make simulate` alone, which breaks the rule that `make demo` finishes in 10 minutes before incident management and reconciliation add their own dbt runs. A demo that takes too long does not get run, and a project nobody runs convinces nobody.

## Decision

dbt runs for real every 28 simulated days (days 0, 28, 56 and 84). On every other day the latest real build is replayed: the same models and tests, with the same compiled SQL, stamped with that day's 02:00 run time and priced on that day's table sizes. Raw data arrives every simulated day, including a few in-place changes to products and customers, and each day's sizes and append-only behaviour are recorded. Dashboards refresh every 12 or 24 hours depending on their maturity and read every table they depend on. Five people run up to three ad hoc queries a day from a set of templates per team, including `SELECT *` on large tables and repeated date filters. Results are fetched one page at a time, as a SQL editor does.

The daily work is kept cheap on purpose. Each day's raw load only generates the ids whose event time can fall in that day's window, since event time rises with the id, instead of evaluating the whole id space: about 0.4 seconds a day at scale 1.0, down from 1.1. Everything between two real builds shares one database transaction, because every commit forces DuckDB's write-ahead log to disk. Inserts use multi-row `VALUES` statements, which measured 0.035 seconds for 400 rows against 0.17 seconds for `executemany` and about 0.8 seconds through Arrow. A simulation always starts from a fresh seed, and anything that looks random comes from a stable SHA-256 hash of the seed, never from Python's per-process `hash()` or the wall clock.

## Consequences

Raw data, which grows every day, is always current. Model sizes only refresh when dbt really builds, so a replayed day prices model reads on sizes up to four weeks old, and dashboards read marts up to four weeks stale. For cost attribution, where the question is who spends what over a quarter, that is an acceptable trade for a demo people will actually run. The report states how many builds were real and how many replayed, `ops.query_log` marks replayed rows, and only real builds are used where the executed SQL matters, such as checking that parsed lineage matches dbt's.

Loading by window exposed a real defect. A payment could be stamped as loaded minutes before its order, so a load boundary falling in between dropped the payment for good. Payments are now never loaded before their order, and tests check both that invariant and that many unevenly spaced appends give exactly the same tables as a single load.

If the budget tightens further, the lever is `workload.real_build_every_days`, not the data volume, since dbt's time barely depends on volume.
