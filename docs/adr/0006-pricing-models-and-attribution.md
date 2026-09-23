# 0006: Pluggable pricing, and charging each query exactly once

Status: accepted
Date: 2026-09-23

## Context

The same workload costs very different amounts under the two pricing styles cloud warehouses use. On-demand scan pricing charges for bytes read. Warehouse pricing charges for the time a warehouse runs, including the minutes it sits idle before suspending. Which one an organisation is on changes which habits are expensive, so the report has to show both side by side. It also has to stay usable when a real bill replaces the simulated one: the SPEC wants a BigQuery or Snowflake adapter to slot in without touching attribution or recommendations.

Attribution has an organisational question at its core: who pays for a query? If every cost lands on the team that owns the table, the platform team, which owns the whole raw and staging layer, absorbs everyone's `SELECT *` and has no lever to change it. If every cost lands on the reader, nobody pays for building the tables at all. Charging a query twice, once to each, makes team totals exceed the bill and invites arguments about double counting instead of about waste.

## Decision

Collection, pricing and attribution are separate steps. Collectors emit `QueryRecord` rows; a `PricingModel` turns estimated queries into `PricedQuery` rows; attribution and recommendations read only priced rows. Two models ship:

- `ScanPricing`: USD per TiB of bytes scanned, with BigQuery's minimum of 10 MB billed per table referenced.
- `ComputePricing`: USD per credit-hour. Each workload (dbt, dashboards, ad hoc) runs on its own extra-small warehouse. Queries queue one at a time; a warehouse starts on the first query, bills at least 60 seconds per start, and keeps billing until it has been idle for 300 seconds. Each running period's cost is shared by its queries in proportion to their modelled duration, so idle time is charged to the workload that caused it.

Every query is charged exactly once. A dbt build is production cost for the owner of the model it writes. A dbt test is production cost for the owner of the model it tests, because testing is part of what publishing that model costs. A dashboard refresh is consumption cost for the dashboard owner's team, and an ad hoc query is consumption cost for the team of the person who ran it. Money is kept as `Decimal` and summed in SQL over `DECIMAL` columns, so team totals equal the bill to the last digit, which an end-to-end test checks.

## Consequences

The side-by-side comparison exposes a real trade-off. Over the full 13-week run at scale 1.0, dashboards cost $0.65 under scan pricing and $167.90 under compute pricing, with 99% of the billed warehouse time idle, because each refresh wakes the BI warehouse for a few seconds of work and five minutes of idling. The twelve abandoned models, by contrast, would save about $0.35 a month. That is the argument for consolidating refresh schedules or moving BI to on-demand pricing, and the report makes it with the organisation's own numbers.

Charging readers for reads gives each team a lever it controls. A team that wants a smaller bill can narrow its dashboards' columns or refresh them less often, and a team that owns an expensive model can make it incremental. Neither can push its cost onto the other. The platform team's consumption line stays small because it rarely reads other teams' data; its production line is large because it builds the shared layers. That split is the honest picture of a central team, and the ownership registry is what makes it possible.

A real vendor bill can replace either model. An adapter that returns `PricedQuery` rows from a billing export plugs in where the simulated pricing sits, and everything downstream is unchanged.
