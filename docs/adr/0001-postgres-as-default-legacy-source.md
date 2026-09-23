# 0001: Postgres as the default legacy source, SQL Server behind a profile

Status: accepted
Date: 2026-09-23

## Context

The reconciliation module verifies a legacy-to-lakehouse migration, and the interesting bugs it has to catch are the ones that come from the source engine's type system: `NUMERIC` scales, naive timestamps stored as local time, `VARCHAR` with trailing spaces, case-insensitive collation. SQL Server is the more realistic legacy system for that story, and its case-insensitive default collation is a discrepancy class that Postgres will not produce on its own.

The practical problem is that the official SQL Server 2022 image is published for x86_64 only. On Apple Silicon it runs under emulation, where it is slow and sometimes fails to boot at all. If reconciliation depended on it, then whether a contributor could work on the module would come down to which laptop they were issued. That turns a hardware accident into a staffing constraint: the one person with the right machine becomes the bottleneck for an entire module, and review of their work gets harder because nobody else can reproduce a failure locally. The same logic applies to CI, which should not need a heavyweight database container to tell you whether the diff algorithm is correct.

## Decision

Postgres is the default legacy source and always starts with `make up`. SQL Server is defined in the same `docker-compose.yml` but sits behind a Compose profile named `sqlserver`, so it starts only when someone asks for it with `docker compose --profile sqlserver up -d`.

The reconciliation code reaches both through the same `SourceConnector` interface. Engine-specific behaviour (the checksum expression, the collation-driven case folding rule) lives in the connector implementation and in per-table canonicalization config, not in the diff algorithm. `make reconcile` therefore runs end to end against Postgres by default, and enabling the profile adds a second engine to the same test rather than switching modes.

## Consequences

Anyone can clone the repo and run the full reconciliation path on any machine, so the module has no hardware gate and review is not concentrated on one person. The cross-engine hash requirement stays honest, because the canonicalization and checksum layers are still written against two engines even when only one of them is running.

The cost is that the SQL Server path gets less routine exercise: a defect that only shows up under case-insensitive collation can sit unnoticed until someone enables the profile. Two things keep that bounded. The cross-engine golden-row test that proves hash equality is written so it skips rather than passes when the profile is off, so a skipped test is visible in the output instead of looking green. And the case folding rule is configuration, so the Postgres run can exercise the same code path by turning folding on explicitly, even though Postgres would not require it.

If the project ever needs SQL Server to be the default, the change is which service carries the profile marker, not a change to the diff.
