# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 0: Scaffolding. Planned and approved, not yet implemented.
Branch: `phase-0-scaffolding` (off `main`).

## Done

- Read `docs/CLAUDE.md` and `docs/SPEC.md` in full.
- Created branch `phase-0-scaffolding`.
- Created this file.
- Wrote and got approval for the Phase 0 implementation plan (file list, assumptions, open questions, verification steps).

## In progress

Nothing. Waiting for the go-ahead to write the Phase 0 files.

## Decisions made

1. **GNU make will be installed via winget** (`ezwinports.make`, fallback `GnuWin32.Make`). This dev machine has no `make`, and no choco or scoop. Phase 0 acceptance in `docs/SPEC.md` is literally `make setup && make up && make test && make lint`, so the Makefile stays exactly as specified rather than being replaced by a Windows-only shim. Makefile recipes will be pinned to bash so the same file works unchanged on Linux and macOS.
2. **`CLAUDE.md` stays at `docs/CLAUDE.md`.** The target layout in `docs/SPEC.md` shows it at the repo root. Owner chose to keep the file where it is, so the layout diagram in SPEC.md gets corrected instead.
3. **Phase 0 builds all four `common/` modules**: config loading, DuckDB connection, logging, and the simulated clock. The Phase 0 text in SPEC.md names only `common.clock`, but the target layout lists the other three, and Phase 1 needs all of them. They will be thin but real, with unit tests.
4. **Placeholder CLI subcommands exit non-zero** with a "not implemented until phase N" message, so a partly built pipeline cannot report success.

## Known issues

- `make demo` will fail for the whole of Phase 0 by design, because every module subcommand is a placeholder that exits non-zero. Expected, not a bug.
- `dbt-duckdb`, `pyiceberg` and `streamlit` in one dependency resolution may conflict. Plan is a single flat dependency set, splitting into extras only if `uv lock` actually fails. Not yet attempted.
- Local environment: uv 0.11.14, Python 3.11.9, Docker 29.8.0, Docker Compose v5.5.1. `make` absent until step 1 of the plan runs.

## Open questions for the owner

Defaults are listed for each. These do not block starting Phase 0.

1. Python pin: `>=3.11` as written in SPEC, or `>=3.11,<3.13`? `dbt-duckdb` tends to lag new Python releases. Default: `>=3.11,<3.13`.
2. Dependencies: one flat set, or optional extras (`[dashboard]`, `[legacy]`) if resolution conflicts? Default: flat, split only on failure.
3. CLI shape: sub-apps everywhere (`platform-ops cost report`, matching the documented `platform-ops metadata check`), or flat verbs matching Makefile target names? Default: sub-apps.
4. `dbt/` in Phase 0: empty directory, or a minimal `dbt_project.yml` and `profiles.yml` so the dbt toolchain is proven early? Default: empty directory.
5. CI: add a lint and test GitHub Actions workflow now, or wait for Phase 5 as SPEC says? Default: wait.
6. `.env` bootstrap: should `make up` fail loudly when `.env` is missing, or auto-copy `.env.example`? Default: fail loudly.

## Next step

Implement Phase 0 on this branch, in this order:

1. `winget install ezwinports.make`, confirm `make --version`.
2. `.gitignore`, `.env.example`, `pyproject.toml`, then `uv lock` and resolve any dependency conflict.
3. `src/platform_ops/common/{config,clock,db,logging}.py` plus the empty module packages.
4. `src/platform_ops/cli.py` with placeholder subcommands.
5. `config/{settings,teams,ownership}.yaml`.
6. `Makefile`, `docker-compose.yml`.
7. `tests/` covering config, clock, db and the CLI surface, all without Docker.
8. `docs/adr/0001-postgres-as-default-legacy-source.md`, and fix the layout diagram in `docs/SPEC.md`.
9. Run the full acceptance line: `make setup && make up && make test && make lint`, plus `platform-ops --help`.
10. Update this file with the real results, push the branch, leave the merge to the owner.

Do not start Phase 1.
