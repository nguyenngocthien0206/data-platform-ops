# 0013: CI and release from the same container, the tag pushed by a person

Status: accepted
Date: 2026-09-29

## Context

Before Phase 8, CI ran natively with uv and without Docker, so every Postgres test skipped there and CI checked a different environment from the one people now ran (ADR 0012). There was no release: no version anyone could name, no image anyone could pull. For a toolkit that other teams are meant to adopt, both matter. A team deciding whether to depend on it needs to know that what passed CI is what they will run, and which version they are on when they report a problem.

## Decision

- **CI calls the same `docker-*` make targets people run**, on every pull request and push to `main`: the image is built with the GitHub Actions cache, then lint and type check, the ownership check, and the tests against Postgres from Compose. The native uv job is gone. CI and a laptop cannot drift apart when they run the same commands.
- **SQL Server runs in its own job, on demand** (`workflow_dispatch`), because its image is large and slow to start. That job runs the full suite with both engines and fails on any skip, since a skip there means an engine was unreachable.
- **A release is a pushed `v*` tag, and a person pushes it.** The release workflow reruns the full CI, checks that the tag equals the version in `pyproject.toml`, pushes the amd64 image to GitHub Container Registry (`1.0.0`, `1.0`, `latest`), and creates a GitHub Release whose notes are that version's section of `CHANGELOG.md`. Nothing is published on ordinary pushes.
- **The version is written in one place.** `pyproject.toml` holds it, the package reads it from its installed metadata, and a test keeps the version, the package and the changelog in agreement on every pull request, long before anyone tags.
- **Type checking uses mypy's compiled wheels.** The pure-Python build existed only for Smart App Control; a cold `make docker-lint` went from 3 minutes 48 seconds to about a minute.
- **The image is amd64 only**, like the SQL Server image it runs next to.

## Consequences

The pull request that introduced this passed CI in about six minutes (tests against Postgres 3 minutes 31 seconds). A published image runs the demo with `TOOLKIT_IMAGE=ghcr.io/nguyenngocthien0206/data-platform-ops:1.0.0 make docker-demo`, and a local replay of that path wrote reports byte-identical to the Phase 7 baseline.

Keeping the tag manual is a deliberate gate: publishing is a decision about what other teams will run, so it belongs to the owner, not to a merge. The costs are explicit. SQL Server regressions are only caught when someone runs the on-demand job, so it has to be part of the release routine. Apple Silicon runs the image under emulation. And the GHCR package may start private, which the owner fixes once in its settings. The browser check and `readme-check` stay out of CI: the first needs a 2 GB browser image, the second a full demo run on every pull request.
