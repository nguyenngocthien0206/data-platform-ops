# Architecture decision records

Each choice that shapes a number, a boundary or how the toolkit is run is recorded here, with what it costs. The format is short: Context, Decision, Consequences. Records are not rewritten when a later phase changes their context; a "Later note" at the end points to the newer decision instead.

| ADR | Decision | Phase |
|---|---|---|
| [0001](0001-postgres-as-default-legacy-source.md) | Postgres as the default legacy source, SQL Server behind a profile | 0 |
| [0002](0002-ownership-resolution-and-platform-team.md) | Most specific ownership rule wins; the platform team owns the shared layers | 1 |
| [0003](0003-source-freshness-on-simulated-time.md) | Source freshness on simulated time | 1 |
| [0004](0004-deterministic-data-generation.md) | Deterministic data generation | 1 |
| [0005](0005-scan-estimate-and-modelled-compute-time.md) | Bytes from logical sizes, compute time from a model | 2 |
| [0006](0006-pricing-models-and-attribution.md) | Pluggable pricing, each query charged exactly once | 2 |
| [0007](0007-simulated-workload-real-builds-daily-replay.md) | Real dbt builds every four weeks, daily runs replayed | 2 |
| [0008](0008-incident-grouping-severity-and-routing.md) | Incident grouping, severity and routing | 3 |
| [0009](0009-cross-engine-hashing-and-canonicalization.md) | Cross-engine hashing and canonicalization | 4 |
| [0010](0010-local-first-design.md) | Local first, every external system behind an interface | 5 |
| [0011](0011-module-boundaries-and-a-review-before-release.md) | Module boundaries pinned by a test, and a review before the release | 6 |
| [0012](0012-the-toolkit-in-a-container.md) | The toolkit in a container, with its state on volumes | 7 |
| [0013](0013-ci-and-release-from-the-same-container.md) | CI and release from the same container, the tag pushed by a person | 8 |

To add one, take the next number, name the file `NNNN-short-title.md`, start with `Status` and `Date`, and add a row here.
