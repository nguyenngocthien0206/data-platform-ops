# 0010: Local first, with every external system behind a small interface

Status: accepted
Date: 2026-09-25

## Context

The three problems this toolkit works on, cost showback, incident routing and migration sign-off, normally live on a cloud warehouse, a paging service and two production databases. Building against those directly would make the project honest about its environment and useless to almost everyone who opens it: a reviewer would need a billing account, credentials and a week of real query history before seeing a single number. A demo that cannot be run gets skimmed, and the ideas in it get judged on trust instead of evidence.

There is an operational side too. A platform team that can reproduce an incident, a bill or a failed migration on a laptop fixes things faster than one that has to wait for access to production, and new people learn the system by running it rather than by reading about it.

## Decision

Everything runs on one laptop, offline after `make setup`, from one command:

- **The warehouse is a DuckDB file**, transformed by dbt through `dbt-duckdb`, around 120 models of a simulated company.
- **Legacy sources are Docker containers**: Postgres by default, SQL Server behind a Compose profile (ADR 0001).
- **The lakehouse is Iceberg** through PyIceberg, with a SQLite catalog and a local directory as the warehouse.
- **Time is simulated** (`common.clock`), so thirteen weeks of workload or three weeks of incidents take minutes.
- **Anything random comes from a fixed seed** through a stable hash (ADR 0004), so two runs of `make demo` write the same reports.
- **Nothing phones home.** dbt's anonymous usage stats and Streamlit's usage stats are both switched off in the repo's config.

Each place where a real deployment would talk to an outside system sits behind a small interface with a local default:

| Interface | Local default | What a real deployment plugs in |
|---|---|---|
| `QueryCollector` (`cost/collect.py`) | `LoggedConnection` and the dbt run reader | `BigQueryJobsCollector`, `SnowflakeQueryHistoryCollector` over the vendor's query history |
| `PricingModel` (`cost/pricing.py`) | `ScanPricing`, `ComputePricing` on logical bytes and modelled time (ADR 0005, 0006) | the vendor's measured bytes (`QueryRecord.bytes_scanned`) or a billing export |
| `Notifier` (`incidents/notify.py`) | `LocalNotifier`: `ops.notifications` and the log | `SlackNotifier` when a webhook is configured; a paging service the same way |
| `SourceConnector` (`reconcile/connectors.py`) | Postgres, SQL Server and DuckDB | any engine that can render the canonical row string and compute MD5 (ADR 0009) |

The two cloud collectors are real code, but they are verified only by contract tests against recorded rows in the vendors' documented schemas (`tests/fixtures/`, written by `scripts/make_vendor_fixtures.py` from the simulated workload). They never touch the network, and no test needs an account.

Every module writes its results to the `ops` schema, and the dashboards read nothing else. The storage layer is the one place a team would swap in their own warehouse without touching the logic.

## Consequences

Anyone with Docker and 16 GB of memory can check every claim in the READMEs by running `make demo` and `make readme-check`. The price is that no number here is a real bill or a real incident history: bytes are logical estimates, compute time is modelled, and faults are planted. The reports say so, and the ADRs record each simplification where it was made, so a reader knows which conclusions carry over (who pays, what grouping saves, when a segmented diff pays off) and which figures would move on real data (the dollar amounts).

Running on a laptop also means running under whatever the laptop enforces. On the development machine, Windows Smart App Control started refusing freshly released native wheels during Phase 5 (pandas 3.0.6 and pyarrow 25.0.1), so both are capped at versions that load. That is a cost of the local-first choice worth stating: a managed laptop is an environment with its own policies, and the toolkit has to live within them rather than ask people to switch them off.

Adding a real system is additive. A cloud collector, a vendor billing adapter or a paging notifier implements an existing interface and is chosen in configuration; the attribution, grouping and reconciliation logic, and the tests that pin it, stay as they are.
