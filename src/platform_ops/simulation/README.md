# simulation

Everything that makes the simulated company behave like a real one: the raw data generator and mart rollup codegen (Phase 1), and the workload generator (Phase 2). Fault injectors (Phases 3 and 4) will live here too.

## Raw data (`raw_data.py`)

`platform-ops seed` writes nine tables into the `raw` schema of the DuckDB warehouse: customers, products, orders, order items, payments, web sessions, marketing campaigns, marketing spend and support tickets. Every value comes from a hash of the seed, table, row id and column, generated with SQL inside DuckDB and inserted in primary-key order. That combination makes the content of two runs identical, row for row. The on-disk compression is a different matter: DuckDB can pick a different codec for the same column on two runs, so compressed table sizes are not stable and should not be priced directly. The reasoning is in [ADR 0004](../../../docs/adr/0004-deterministic-data-generation.md).

The data is deliberately messy, because the metadata, cost and incident tooling is only worth building if it has real problems to find. Some customers register twice with the same email in a different case or with stray whitespace, so staging has to fold them before any per-customer number is right. About 5% of payments reach the warehouse one to five days after they happened, which is why finance reconciliation distinguishes a missing payment from a late one. Phones, referral sources and most session user ids are nullable, and every table carries a `_loaded_at` column so freshness can be measured.

Each table is defined over a fixed id space that spans the history and the whole simulated window, and a load inserts the rows whose `_loaded_at` falls in `(after, until]`. `HISTORY_COUNTS` sets how big the company is by the window start, so a longer simulated window adds rows after the start rather than shrinking the history. The seed is one load up to the window start; the workload appends one simulated day at a time with the same function, generating only the ids that can land in that day. A payment is never loaded before its order, so no load boundary can lose one. Tests check that invariant, and that many unevenly spaced appends produce exactly the same tables as a single load.

During the simulated window, `apply_daily_changes` also changes some existing rows in place: a few product prices and customer profiles each day, stamped with a new `_loaded_at` the way change data capture would. Every other source only ever gains rows. That difference is what lets the cost module tell which models could safely be built incrementally.

### Seed results at scale 1.0

From an actual `make seed` run with the committed `config/settings.yaml`:

| Table | Rows |
|---|---:|
| customers | 48,803 |
| products | 2,000 |
| orders | 399,975 |
| order_items | 1,198,359 |
| payments | 371,450 |
| web_sessions | 1,449,825 |
| marketing_campaigns | 80 |
| marketing_spend | 10,018 |
| support_tickets | 27,355 |
| **total** | **3,507,865** |

Generation took 5.2 seconds in-process, and `make seed` 8 seconds including startup. Of the 371,450 payments, 18,472 arrived more than a day late, and deduplication folded 806 duplicate customer accounts. Two clean runs of `make seed && make build` gave identical content in all 127 tables (9 raw, 118 models), compared by row count and a hash over every row.

Row counts scale linearly with `scale_factor`, with small floors so tests at scale 0.01 still have enough rows to be meaningful.

## Mart rollups (`codegen.py`)

Each business team has a handful of hand-written core facts. Dashboards want the same numbers cut by day, week and month and by one dimension or another, which is mechanical work, so `codegen.py` generates those cuts from a short declarative spec: 12 families producing 57 models. The output is committed because dbt reads models from disk. Regenerate after changing the spec:

```
uv run python -m platform_ops.simulation.codegen
```

A test compares the committed files with a fresh render, so a hand edit to a generated model fails CI instead of drifting from the spec.

## Workload (`workload.py`)

`platform-ops simulation run` simulates 13 weeks of the platform in a few minutes, always from a fresh seed. Every simulated day, new raw data arrives and some products and customers change; the raw sources are measured and checked for append-only growth; dbt runs at 02:00, for real every 28 days and replayed from the latest real build on the other days; the 12 dashboards refresh every 12 or 24 hours, reading every table they depend on; and five people run ad hoc SQL during office hours, from careful queries to `SELECT *` on large tables. Nobody's queries touch the abandoned models, which is exactly what makes them waste. The design and its trade-offs are in [ADR 0007](../../../docs/adr/0007-simulated-workload-real-builds-daily-replay.md).

### Workload results at scale 1.0

From two actual runs of `make simulate`: 91 days, 4 real dbt builds and 87 replayed, 24,934 dbt queries, 10,192 dashboard queries and 687 ad hoc queries, 5,427,803 raw rows loaded and 3,497 changed in place. The runs took 280 and 290 seconds, and collected the same workload both times.
