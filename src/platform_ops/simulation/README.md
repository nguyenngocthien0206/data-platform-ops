# simulation

Everything that makes the simulated company behave like a real one. Phase 1 builds the raw data generator and the mart rollup codegen. The workload generator (Phase 2) and fault injectors (Phases 3 and 4) will live here too.

## Raw data (`raw_data.py`)

`platform-ops seed` writes nine tables into the `raw` schema of the DuckDB warehouse: customers, products, orders, order items, payments, web sessions, marketing campaigns, marketing spend and support tickets. Every value comes from a hash of the seed, table, row id and column, generated with SQL inside DuckDB and inserted in primary-key order. That combination makes the content of two runs identical, row for row. The on-disk compression is a different matter: DuckDB can pick a different codec for the same column on two runs, so compressed table sizes are not stable and should not be priced directly. The reasoning is in [ADR 0004](../../../docs/adr/0004-deterministic-data-generation.md).

The data is deliberately messy, because the metadata and incident tooling is only worth building if it has real problems to find. Some customers register twice with the same email in a different case or with stray whitespace, so staging has to fold them before any per-customer number is right. About 5% of payments reach the warehouse one to five days after they happened, which is why finance reconciliation distinguishes a missing payment from a late one. Phones, referral sources and most session user ids are nullable, and every table carries a `_loaded_at` column so freshness can be measured.

Each table is defined over a fixed id space that spans the history and the whole simulated window, and a load inserts the rows whose `_loaded_at` falls in `(after, until]`. The initial seed is one load up to the window start. Phase 2 will append one simulated day at a time with the same function, and a test proves that seeding through a day and appending the next gives exactly the same tables as seeding straight through both.

### Results at scale 1.0

From an actual `make seed` run with the committed `config/settings.yaml`:

| Table | Rows |
|---|---:|
| customers | 49,200 |
| products | 2,000 |
| orders | 403,009 |
| order_items | 1,207,316 |
| payments | 374,258 |
| web_sessions | 1,450,746 |
| marketing_campaigns | 81 |
| marketing_spend | 10,092 |
| support_tickets | 28,340 |
| **total** | **3,525,042** |

Generation took 3.8 seconds in-process, and `make seed` about 6 seconds including startup. Of the 374,258 payments, 18,609 arrived more than a day late, and deduplication folded 838 duplicate customer accounts.

Row counts scale linearly with `scale_factor`, with small floors so tests at scale 0.01 still have enough rows to be meaningful.

## Mart rollups (`codegen.py`)

Each business team has a handful of hand-written core facts. Dashboards want the same numbers cut by day, week and month and by one dimension or another, which is mechanical work, so `codegen.py` generates those cuts from a short declarative spec: 12 families producing 57 models. The output is committed because dbt reads models from disk. Regenerate after changing the spec:

```
uv run python -m platform_ops.simulation.codegen
```

A test compares the committed files with a fresh render, so a hand edit to a generated model fails CI instead of drifting from the spec.
