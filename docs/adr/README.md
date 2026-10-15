# Architecture decision records

Each choice that shapes a number, a boundary or how the toolkit is run is recorded here, with what it costs. The format is short: Context, Decision, Consequences. Records are not rewritten when a later phase changes their context; a "Later note" at the end points to the newer decision instead.

| ADR | Decision | Phase |
|---|---|---|
| [0001](0001-postgres-as-default-legacy-source.md) | Postgres as the default legacy source, SQL Server behind a profile (superseded in 2.0) | 0 |
| [0002](0002-ownership-resolution-and-platform-team.md) | Most specific ownership rule wins; the platform team owns the shared layers | 1 |
| [0003](0003-source-freshness-on-simulated-time.md) | Source freshness on simulated time | 1 |
| [0004](0004-deterministic-data-generation.md) | Deterministic data generation | 1 |
| [0005](0005-scan-estimate-and-modelled-compute-time.md) | Bytes from logical sizes, compute time from a model (superseded in 2.0) | 2 |
| [0006](0006-pricing-models-and-attribution.md) | Pluggable pricing, each query charged exactly once | 2 |
| [0007](0007-simulated-workload-real-builds-daily-replay.md) | Real dbt builds every four weeks, daily runs replayed (superseded in 2.0) | 2 |
| [0008](0008-incident-grouping-severity-and-routing.md) | Incident grouping, severity and routing | 3 |
| [0009](0009-cross-engine-hashing-and-canonicalization.md) | Cross-engine hashing and canonicalization | 4 |
| [0010](0010-local-first-design.md) | Local first, every external system behind an interface | 5 |
| [0011](0011-module-boundaries-and-a-review-before-release.md) | Module boundaries pinned by a test, and a review before the release | 6 |
| [0012](0012-the-toolkit-in-a-container.md) | The toolkit in a container, with its state on volumes | 7 |
| [0013](0013-ci-and-release-from-the-same-container.md) | CI and release from the same container, the tag pushed by a person | 8 |
| [0014](0014-the-ownership-gate-in-the-consuming-repository.md) | The ownership gate runs in the consuming repository, on its manifest | 10 |
| [0015](0015-clickhouse-modelling-and-watermark-ingestion.md) | ClickHouse modelling per layer, and ingestion by watermark | 11a |
| [0016](0016-users-per-team-and-bi-from-exposures.md) | One ClickHouse user per team, and the BI built from the dbt exposures | 11b |
| [0017](0017-the-parallel-run-and-the-cutover-rule.md) | The migration is signed off by a parallel run, inside the company's own history | 12 |
| [0018](0018-finops-on-a-self-managed-clickhouse.md) | FinOps on a self-managed ClickHouse: the stated bill shared out by measured reads | 13 |
| [0019](0019-incidents-on-the-companys-production-runs.md) | Incidents on the company's own production runs, from a store any run can feed | 14a |
| [0020](0020-orchestrators-call-the-toolkit.md) | Orchestrators call the toolkit through its CLI, each in its own image and namespace | 14b |

To add one, take the next number, name the file `NNNN-short-title.md`, start with `Status` and `Date`, and add a row here.
