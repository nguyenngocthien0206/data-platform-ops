# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 0: Scaffolding. Implemented, acceptance criteria verified locally.
Branch: `phase-0-scaffolding`. Not merged, awaiting review.

## Done

Phase 0 is complete and the acceptance line passes on a clean state:

```
make setup && make up && make test && make lint
```

Built:

- `pyproject.toml`: single dependency set, `uv` managed, hatchling build, src layout, ruff and mypy config. Resolves to 113 packages with no conflict.
- `Makefile`: every target from the SPEC table. `setup`, `up`, `down`, `test`, `lint`, `fmt`, `clean` are real; `seed`, `build`, `simulate`, `cost`, `incidents`, `reconcile`, `dashboard`, `demo` delegate to the CLI.
- `docker-compose.yml`: `postgres:16` (default, healthchecked) and SQL Server 2022 behind the `sqlserver` profile.
- `.gitignore`, `.env.example`.
- `config/settings.yaml` (real and loaded), `config/teams.yaml` (the four teams), `config/ownership.yaml` (stub, Phase 1 fills it).
- `src/platform_ops/common/`: `config.py` (pydantic, extra keys forbidden), `clock.py` (`Clock` protocol, `SystemClock`, `SimulatedClock`), `db.py` (DuckDB, creates `raw` and `ops`), `logging.py` (records carry real and simulated time).
- `src/platform_ops/cli.py`: `platform-ops` with sub-apps `metadata`, `simulation`, `cost`, `incidents`, `reconcile` plus `seed`, `build`, `dashboard`, `version`, `config`.
- Empty package markers for `metadata`, `simulation`, `cost`, `incidents`, `reconcile`.
- `tests/`: 47 tests covering config validation, clock determinism, DuckDB schema setup and the CLI surface. No Docker, no network.
- `docs/adr/0001-postgres-as-default-legacy-source.md`.

### Verified results from the actual run

| Check | Result |
|---|---|
| `make setup` | passes, 113 packages resolved, no dependency conflict |
| `make up` | `dpo-postgres` reports healthy |
| `make test` | 47 passed in 1.64s |
| `make lint` | ruff check clean, 21 files formatted, mypy clean on 12 source files |
| `platform-ops --help` | lists all 10 commands and command groups |
| placeholder command | `platform-ops cost report` exits 1 with a phase 2 message |
| clock determinism | two clocks, 42 simulated daily runs, identical sequences, span 41 days |
| compose profiles | default resolves to `postgres` only; `--profile sqlserver` resolves to both |

Key resolved versions: duckdb 1.5.5, dbt-duckdb 1.11.0, sqlglot 30.19.0, networkx 3.6.1, pydantic 2.13.5, typer 0.27.2, pyiceberg 0.12.0, psycopg 3.3.6, streamlit 1.64.0, ruff 0.16.8, mypy 1.19.1, pytest 9.1.1.

## In progress

Nothing. Phase 0 is finished and waiting for review and merge.

## Decisions made

1. **GNU make installed via winget** (`ezwinports.make`, 4.4.1). The machine had no `make` and no choco or scoop. The Makefile is exactly as the SPEC requires, with recipes pinned to bash so the same file works on Linux and macOS.
2. **`CLAUDE.md` stays at `docs/CLAUDE.md`.** The layout diagram in `docs/SPEC.md` was corrected instead of moving the file. The diagram now also shows `data/`, `warehouse/`, `reports/` and `.env.example`, which the SPEC referenced but did not list.
3. **All four `common/` modules built in Phase 0**, not just the clock. Phase 1 needs config, DuckDB access and logging on day one.
4. **Placeholder CLI subcommands exit non-zero** with a "not implemented until phase N" message, so a half-built pipeline cannot report success. `make demo` therefore fails for the whole of Phase 0, by design.
5. **`mypy` pinned `>=1.10,<1.20` and built from source** (`[tool.uv] no-binary-package = ["mypy"]`). This machine runs a Windows Application Control policy that refuses to load freshly downloaded, low-reputation native extensions. It blocked the mypyc-compiled mypy wheel, the generated `mypy.exe` shim, and the native `librt` package that mypy 1.20 and later depend on. Pure-Python mypy below 1.20 has no native dependency and runs fine. The Makefile calls `python -m mypy` rather than the `mypy` shim for the same reason. **This is the one decision that constrains the project because of one machine, so it is worth your review.** See known issues.
6. **Defaults applied for the six open questions** left unanswered from the Phase 0 plan: Python pinned `>=3.11,<3.13`; one flat dependency set (no extras needed, resolution was clean); CLI uses sub-apps; `dbt/` is an empty directory; no CI workflow yet; `make up` fails loudly when `.env` is missing.

## Known issues

- **mypy version cap is a local constraint with a global effect.** Capping at `<1.20` and building from source is what makes `make lint` pass on this machine. On an unrestricted machine the cap is unnecessary and costs a few seconds of build time per sync. If the policy is relaxed, or if the project moves to a machine without it, drop both the cap and the `no-binary-package` entry.
- **SQL Server was never booted.** The `sqlserver` profile was validated with `docker compose --profile sqlserver config` only, which proves the service is wired and the file parses. The image itself was not pulled or started. First real use is Phase 4.
- **`make demo` fails** for the whole of Phase 0, because every module subcommand is a placeholder. Expected, not a bug.
- **`uv` warns about `VIRTUAL_ENV`** if a parent shell has another virtualenv active (for example `workspace/.venv`). uv correctly ignores it and uses the project `.venv`, but the warning is noisy. Deactivate the outer venv to silence it.
- `make` is on PATH only in shells started after the winget install.

## Open questions for the owner

1. **The mypy cap** in decision 5 above. Accept it, or would you rather keep mypy unconstrained and let `make lint` skip type checking on this machine?
2. **CI timing.** The SPEC puts GitHub Actions in Phase 5. A lint and test workflow added now would keep every later branch honest from the start. Still worth deferring?
3. **`dbt/` is empty**, so `make build` fails until Phase 1. Confirm that is the right reading rather than scaffolding a minimal `dbt_project.yml` early.

## Next step

Review and merge `phase-0-scaffolding` into `main`, then start Phase 1: simulated company and shared metadata layer.

Phase 1 delivers, per `docs/SPEC.md`:

1. `simulation/raw_data.py`: seeded e-commerce dataset into the `raw` schema with deliberate mess and `_loaded_at` columns.
2. `dbt/`: around 120 dbt-duckdb models across `staging`, `intermediate`, four `marts` team folders, and 10 to 15 `abandoned` models, plus tests, source freshness, around 12 exposures, and the `query-comment` config with `append: true`.
3. `config/ownership.yaml` and `metadata/registry.py`: pydantic-validated ownership with most-specific-match-wins, plus `platform-ops metadata check`.
4. `metadata/lineage.py`: NetworkX graph from `manifest.json`, persisted to `ops.lineage_edges`.

Phase 1 acceptance: `make seed && make build` succeeds, `platform-ops metadata check` passes, a test proves every abandoned model has zero downstream nodes and zero exposures, and the lineage helpers have unit tests on a hand-built graph.
