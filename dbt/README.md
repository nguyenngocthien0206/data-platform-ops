# dbt project: `company`

The simulated company's transformation layer, built with dbt-duckdb on top of the raw tables that `platform-ops seed` writes. It is shaped like a real multi-team project on purpose, including the parts real projects would rather not have, because the cost and incident modules need realistic ownership boundaries and realistic waste to work on.

## Layers and owners

| Folder | Models | Owned by | What it holds |
|---|---:|---|---|
| `models/staging` | 9 | platform | One model per raw table: rename, cast, fold emails. No business logic. |
| `models/intermediate` | 16 | platform | Shared logic every team relies on: customer deduplication, order money, payment reconciliation, sessions, last-touch attribution, ticket SLAs. |
| `models/marts/<team>` | 24 | each business team | Hand-written core facts and dimensions, six per team. |
| `models/marts/<team>/generated` | 57 | each business team | Day, week and month rollups generated from `platform_ops.simulation.codegen`. Do not edit by hand. |
| `models/abandoned` | 12 | each business team | Models that still build every night but that nothing reads. |

That is 118 models in total. Every model is a full-refresh table, so each one has a real storage size and a real build cost for the cost module to price. Mart, abandoned and exposure names start with the owning team (`finance_fct_revenue`), which lets one ownership rule cover a whole team while narrower rules override it.

The platform team owns everything below the marts. Most root causes surface there, so most incidents will route there too. That concentration is a deliberate choice, recorded in [ADR 0002](../docs/adr/0002-ownership-resolution-and-platform-team.md).

## Abandoned models

The 12 models in `models/abandoned` each carry a short history in a header comment: a first-touch attribution model replaced in 2025-06, a one-off backfill nobody deleted, a proof of concept that outlived its environment. They live in the same `marts` schema as the real marts, so nothing in the warehouse marks them as dead except that nobody reads them. That is how waste hides in a real warehouse, and it is what the cost module has to find.

Two tests keep the setup honest. Every abandoned model has zero downstream nodes and feeds no exposure, which also means none of them has dbt tests, since a test is itself a downstream reader. Every other model reaches at least one exposure, so the cost module's unused list should be exactly the abandoned set.

## Exposures

Twelve exposures stand in for dashboards, three per business team, and they are how lineage reaches the people who consume the data. Each exposure's `owner` must match the ownership registry, and `platform-ops metadata check` fails if the two disagree.

## Conventions worth knowing

- **Query comments.** Every query dbt issues ends with a JSON comment carrying the node id, for example `/* {"app": "platform-ops", "unique_id": "model.company.sales_fct_orders"} */`, so query history can be joined back to models and owners.
- **Freshness on simulated time.** `macros/collect_freshness.sql` measures source freshness against the `simulated_now` var instead of the wall clock, and fails loudly if the var is missing. See [ADR 0003](../docs/adr/0003-source-freshness-on-simulated-time.md).
- **No packages.** The three custom generic tests (`row_count_in_range`, `non_negative`, `unique_combination_of_columns`) live in `tests/generic`, so setup needs no network access. Row count bounds are given at scale 1.0 and scaled by the `scale_factor` var.
- **Generic test arguments** use the `arguments:` block that dbt 1.12 requires.
- **Usage tracking is off** (`flags.send_anonymous_usage_stats: false`), because the toolkit runs offline.
- **Run it through `platform-ops`.** `platform-ops build` sets the warehouse path and vars from `config/settings.yaml`. The profile has no default path of its own, so there is only one source of truth for where data lives.

## Results from an actual run

`make seed && make build` at scale 1.0, on the development laptop:

| | |
|---|---:|
| Models built | 118 |
| Data tests passed | 156 of 156 |
| Exposures | 12 |
| `make build` wall time, including dbt startup and the lineage refresh | 32 to 35 s |
| Warehouse file after a clean seed and build | 293 MB |

Two clean runs of seed and build produced identical content in all 127 tables (9 raw, 118 models), compared by row count and a hash over every row.
