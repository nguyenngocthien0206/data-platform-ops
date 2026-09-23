# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 1: Simulated company and shared metadata layer. Implemented, all acceptance criteria verified locally from a clean state.
Branch: `phase-1-simulated-company` (off `main` at `bdb614f`). PR open, awaiting review.

## Done

Phase 1 acceptance, run from `make clean`:

```
make seed && make build && uv run platform-ops metadata check
make test && make lint
```

| Check | Result |
|---|---|
| `make seed` | 3,525,042 rows across 9 raw tables, about 6 s |
| `make build` | 118 models, 156 of 156 data tests, 12 exposures, 32 to 35 s including lineage refresh |
| `platform-ops metadata check` | 9 of 9 sources, 118 of 118 models, 12 of 12 exposures owned; 0 warnings |
| `platform-ops metadata check --parse` with no `dbt/target` | passes (the CI path, no data needed) |
| Abandoned model acceptance test | every one of the 12 has zero downstream nodes and zero exposures |
| Lineage unit tests on a hand-built graph | pass, including two diamonds and the tie-break |
| `make test` | 104 passed in about 40 s (includes real dbt parse and build at scale 0.01) |
| `make lint` | ruff clean, 40 files formatted, mypy strict clean on 19 source files |
| Determinism | two clean seed and build runs: all 127 tables (9 raw, 118 models) identical by row count and full-row hash |

Built:

- `simulation/raw_data.py`: hash-based generator in DuckDB SQL, rows inserted in key order, loaded by `_loaded_at` window so Phase 2 can append one simulated day with the same function.
- `simulation/codegen.py`: 12 rollup families producing 57 generated mart models, output committed and guarded by a sync test.
- `dbt/`: 9 staging, 16 intermediate, 24 hand-written marts, 57 generated rollups, 12 abandoned models, 12 exposures, 3 custom generic tests, simulated-time freshness override, query comment, schema naming macro.
- `metadata/manifest.py`, `registry.py`, `check.py`, `lineage.py`; `ops.node_ownership` and `ops.lineage_edges`.
- `common/dbt_invoke.py`: in-process dbt via `dbtRunner`, warehouse path and vars always from settings.
- CLI: `seed`, `build`, `metadata check [--parse]`, `metadata lineage [--parse]` are real.
- `.github/workflows/ci.yml`: `make setup`, `make lint`, `make test`, `metadata check --parse`.
- ADRs 0002 (ownership and platform team), 0003 (freshness on simulated time), 0004 (deterministic generation). READMEs for `simulation`, `metadata` and `dbt` with numbers from the actual run.
- `docs/CLAUDE.md` now says "4 business teams plus a data platform team".

## In progress

Nothing. Waiting for review.

## Decisions made

### From plan review (owner)

1. A fifth `platform` team owns every raw source, staging and intermediate model. It ends up owning 34 of 139 datasets, the most of any team.
2. Every model is a full-refresh table.
3. `teams.yaml` has `members`; an owner must be a member of the team on the same rule.
4. Marts, abandoned models and exposures carry a team prefix.
5. Stale ownership rules warn, ambiguous ties fail, one `make test` target runs everything, mypy cap kept, CI added in Phase 1.

### Made during implementation

6. **Abandoned models inherit their team's default tier** instead of `best_effort` as the plan said. Twelve extra rules for dead models would be unrealistic. The exception is `finance_fct_revenue_v0`, which matches the critical revenue glob by name and is demoted by an exact rule. That is the one place "most specific wins" does real work today.
7. **`paths.dbt_target` and `simulation.history_start` added to `settings.yaml`.** The target path makes integration tests run the real CLI against a temp directory. History start was hardcoded before.
8. **`run_dbt` closes dbt-duckdb's cached database handle after every invocation.** dbt-duckdb keeps the DuckDB file open in-process after `dbtRunner.invoke` returns, which blocks read-only connections and other processes. Phases 2 and 3 run dbt repeatedly in one process, so this matters. It uses `DuckDBConnectionManager._ENV`, a private attribute checked against dbt-duckdb 1.11.0; an integration test fails if it stops working.
9. **Writes to `ops` tables go through one transaction** (`common.db.transaction`). Autocommitted `executemany` flushed the write-ahead log per row and took 64 s for 372 lineage edges right after a build; in one transaction it is well under a second, and readers never see a half-written table.
10. **dbt usage tracking is off** in `dbt_project.yml`, because the toolkit runs offline; `dbt/.user.yml` is gitignored as a backstop.
11. **Freshness is off for `products` and `marketing_campaigns`.** The catalogue is a snapshot (newest row 370 days old) and campaigns can go weeks between launches (63 h at the window start), so daily rules there would only produce noise.

### Carried from Phase 0

- GNU make 4.4.1 via winget; Makefile recipes pinned to bash.
- `CLAUDE.md` stays at `docs/CLAUDE.md`.
- Placeholder CLI commands exit non-zero until their phase lands.
- mypy pinned `>=1.10,<1.20`, built from source, called as `python -m mypy`.

## Known issues

- **CI first run on the PR:** setup and lint passed on the Ubuntu runner; tests failed on one flaky test (below), since removed. Needs a green rerun after the fix is pushed.
- **Phase 2 spec question: compressed sizes are not stable.** SPEC Phase 2 says to estimate bytes scanned from "stored sizes (from DuckDB storage metadata)". CI on the first PR caught DuckDB choosing FSST on one run and Dictionary on the next for the same column with identical data; locally it reproduced at 2, 4 and 8 threads, rarely. Content is identical every run, but compressed size can move, and a cost report built on it would break the identical-reports rule. Also, DuckDB 1.5.5's `pragma_storage_info` has no byte-size column at all. Recommendation to discuss at Phase 2 planning: estimate bytes from logical data (row counts times column widths), which is deterministic. This departs from the SPEC wording, so it is the owner's call.
- The flaky test `test_storage_layout_is_identical_across_runs` was removed; it asserted a property DuckDB does not guarantee. Content determinism is still covered by `test_same_seed_produces_identical_content` and the 127-table full-run comparison.
- Three orders reference a customer who signed up a few seconds after the order, an edge of the id-space mapping in the generator. They are harmless and read as realistic mess, but a strict "signup before order" test would catch them.
- `make demo` still fails by design until Phases 2 to 4 replace their placeholder commands.
- `make build` leaves Postgres untouched; it is only needed from Phase 4. It is still running from `make up`; `make down` stops it.
- `uv` prints a `VIRTUAL_ENV` warning if an outer virtualenv is active. Harmless.

## Open questions for the owner

None blocking. Two worth a look during review:

1. Decision 6 (abandoned models keep their team's default tier). Fine, or should each get an explicit `best_effort` rule?
2. Decision 8 relies on a private dbt-duckdb attribute. The alternative is running dbt as a subprocess, which releases the file on exit but loses the structured results Phase 3 wants from `run_results`.

## Next step

Owner reviews and merges `phase-1-simulated-company`, and confirms CI goes green on the first push. Then start Phase 2 (cost attribution) on `phase-2-cost-attribution` off `main`, beginning with the stored-bytes question in Known issues before designing the scan-estimate proxy.
