# 0016: One ClickHouse user per team, and the BI built from the dbt exposures

Status: accepted
Date: 2026-10-13

## Context

After Phase 11a the company ran everything on ClickHouse as one admin account. That hides the two things a platform team most needs to know about its cluster: who ran a query, and who could have. ClickHouse's query log records the user of every query, so with one account Phase 13 could not show any team its own load, and a BI tool connected as admin could read raw customer data and drop tables. The company also had dashboards only on paper: twelve dbt exposures, each listing the models a dashboard reads, with nothing that actually drew them.

The owner chose Metabase as the company's BI, a self-hosted tool a team of this size would really run, and one ClickHouse user per team (both before Phase 11), then settled the details of Phase 11b: a separate `loader` user for the ingestion, one password variable per user, and the Metabase check on demand and before each release rather than in every pull request.

## Decision

- **Eight users, all namespaced like the databases:** one per team in `config/teams.yaml` (`platform`, `sales`, `marketing`, `finance`, `product`), plus `metabase`, `dbt` and `loader` (`simulation/access.py`). The business teams and Metabase read the marts and the reference data; the platform team reads every layer, because it owns them and debugs them; `dbt` reads raw and owns the databases it builds; `loader` may only insert into raw and read Postgres. People and BI use a `reader` settings profile (`readonly = 2`), the pipelines a `writer` profile, both with the session settings of ADR 0015.
- **Least privilege, found by running, not guessed.** dbt also needs `SELECT ON system.parts`, because dbt-clickhouse's `insert_overwrite` finds the partitions to replace there. Metabase's ClickHouse driver looks each of its queries up in `system.processes`; it gets that grant with a row policy limiting it to its own queries, since otherwise the BI tool could read the text of every user's running query.
- **One password variable per user** (`CLICKHOUSE_PASSWORD_<USER>`), with no default. A missing one stops `seed`, `build` and `company run` with exit code 2 before anything is written, naming every missing variable. The admin account creates the users and reads what the toolkit measures, and runs nothing for the company.
- **Metabase behind a Compose profile**, pinned to `metabase/metabase:v0.63.19.6` (its ClickHouse driver is bundled since version 54), with its own state in a `metabase` database on the Postgres server and a 1 GB heap.
- **Dashboards are generated from the exposures** (`simulation/bi.py`): one dashboard per exposure in its owning team's collection, one native SQL card per model it reads. A generated rollup is a line per period and dimension value, a daily one over its last 91 days; anything else is a table sorted by its key; a `dashboard` entry in a model's `config.meta` overrides both. `platform-ops metabase provision` makes Metabase match, updating by name and archiving what no exposure asks for any more, so the BI cannot drift from the project.
- **Checked on demand and before each release**, not in pull requests: `make docker-metabase-check` provisions Metabase, runs every card through its API, fails a chart that reaches Metabase's 2,000-row display limit, and opens every dashboard in headless Chromium with screenshots. The pull request suite runs every card's SQL as the `metabase` user instead, without the JVM.
- **The demo's BI load comes later, from the same SQL.** Phase 13 replays these cards under the `metabase` user, so the BI traffic in the demo's query log and a real Metabase's are one set of queries. A live Metabase's own queries never feed a report.

## Consequences

The query log now says who did what: in the first runs every dbt query is logged under `dbt` with its node comment, and every load under `loader`. That is the raw material for Phase 13's showback by team and for the quotas it will suggest, and it is how a platform team answers "why is the cluster slow at 9 am" with a name rather than a guess.

Ownership of a chart moves to where ownership of the data already lives. Adding a dashboard is adding an exposure, and changing a chart is a `meta` entry next to the model, so both go through the same pull request and the same reviewer as the SQL, and the registry decides which team's collection a dashboard lands in. The rules cover the project without hand-made dashboards; a team that wants a different chart says so in YAML.

Metabase stays out of `make demo`, `make test` and pull request CI. Its image is 1.8 GB and it took 68 seconds to become healthy on the development laptop, so making every change wait for it would teach people to skip the check. The release workflow runs it instead, after a full demo. On the first full run, provisioning took 21 seconds and the check ran all 69 cards of the 12 dashboards without an error. Two limits remain visible on the charts: the last period of every line is partial, because the window ends at 02:00 on its last day, and resource limits per user wait for Phase 13's measurements.
