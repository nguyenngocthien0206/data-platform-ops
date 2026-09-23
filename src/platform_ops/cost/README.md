# cost

Who spends what on the warehouse, and which of it nobody needed to spend. `make simulate` collects 13 weeks of query history from the simulated company; `make cost` prices it under two vendor pricing styles, charges every query to exactly one team, finds waste, and writes `reports/cost.md`.

## How it fits together

Collection, pricing and attribution are separate steps, so a real vendor's query history or bill can replace any one of them without touching the others (ADR 0006).

| Step | Module | Output |
|---|---|---|
| Collect | `collect.py`: `LoggedConnection` for dashboards and ad hoc SQL, `dbt_run_records` for dbt | `ops.query_log` |
| Parse | `sql_parse.py`: tables and columns read, table written, filter columns | `ops.query_tables` |
| Measure | `sizes.py`: uncompressed size of every column, as the data changes | `ops.table_sizes` |
| Observe | `growth.py`: whether each source only gains rows | `ops.source_growth` |
| Estimate | `estimate.py`: bytes scanned and modelled compute time per query | `ops.query_estimates` |
| Price | `pricing.py`: `ScanPricing` and `ComputePricing` behind one `PricingModel` interface | `ops.query_costs` |
| Attribute | `attribution.py`: production cost to the table owner, consumption cost to the reader | `ops.query_attribution`, `ops.cost_by_team` |
| Recommend | `recommend.py`: unused tables, full-scan hotspots, incremental candidates | `ops.cost_report_*` |
| Report | `report.py` | `reports/cost.md`, `reports/cost_proxy_accuracy.md` |

## The decisions that shape the numbers

**Bytes are logical, not stored.** DuckDB can encode the same column differently on two identical runs, so compressed sizes would make the bill move for no reason anyone could act on. A query is billed for the uncompressed size of every column it reads, which is how BigQuery bills on demand. Compute time is modelled as a fixed overhead plus bytes over a throughput, both declared in `config/settings.yaml` and printed in the report (ADR 0005).

**Each query is charged once.** Building and testing a table is production cost for its owner. Reading data, through a dashboard or by hand, is consumption cost for the reader's team. The platform team builds the shared layers and pays for that; it does not pay for other teams' `SELECT *` (ADR 0006).

**Unused means unused all the way down.** A table is unused when neither it nor anything downstream of it was read by a dashboard or a person in the lookback window. dbt reading a table to build another does not count, so a staging model that feeds a live dashboard is never flagged, and one that only feeds dead tables is.

**Incremental is only suggested when it is safe.** The expensive models are checked against their upstream sources, whose append-only behaviour is observed day by day, not assumed. Products and customers change in place during the simulation, so anything built on them is ruled out.

## Results at scale 1.0

From two actual runs of `make simulate && make cost` over the 13-week window. `make cost` took 20 and 24 seconds, priced 35,813 queries, and wrote byte-identical `cost.md` files both times.

| Team | Scan pricing | Compute pricing |
|---|---:|---:|
| finance | $0.66 | $82.16 |
| sales | $0.72 | $76.33 |
| product | $0.51 | $58.00 |
| marketing | $0.63 | $52.97 |
| platform | $0.83 | $26.56 |
| **total** | **$3.35** | **$296.02** |

| Workload | Scan pricing | Compute pricing | Idle share of billed time |
|---|---:|---:|---:|
| dbt builds and tests | $2.55 | $18.01 | 84% |
| dashboards | $0.65 | $167.90 | 99% |
| ad hoc | $0.15 | $110.12 | 100% |

What the numbers say:

- **The platform team is the cheapest team under compute pricing and the most expensive under scan pricing.** It builds the shared layers, which scan the most bytes, but it owns no dashboards, so none of the BI warehouse's idle time, the biggest cost under compute pricing, lands on it.
- **Idle warehouses, not dead tables, are where the money goes.** Every dashboard refresh wakes the BI warehouse for seconds of work and five minutes of idling: $167.90 against $0.65 for the same queries billed on demand. The 12 abandoned models are exactly the unused list at both 30 and 90 days, and together would save about $0.35 a month under compute pricing and $0.06 under scan pricing.
- **Six full-scan hotspots.** The largest is `marts.finance_fct_payments_reconciliation`, filtered by `ordered_at` 182 times by the cash reconciliation dashboard, which scanned 9.64 GB to read a 0.07 GB table again and again.
- **Four incremental candidates**, all staging models on append-only sources: `stg_order_items`, `stg_web_sessions`, `stg_payments` and `stg_orders`. Everything built on `raw.products` or `raw.customers` is ruled out, including the most expensive model, `sales_fct_order_items`.
- **The bytes estimate is conservative.** For ad hoc queries that ran to completion it assumed 234 million rows, about 2.3 times what DuckDB actually scanned, because it bills whole columns while DuckDB skips row groups that a filter rules out. Dashboard queries that returned less than one page, all on small rollup tables, matched exactly.
