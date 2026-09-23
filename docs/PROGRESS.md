# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-23

## Current phase

Phase 1: Simulated company and shared metadata layer. Planned and approved, not yet implemented.
Branch: `phase-1-simulated-company` (off `main` at `bdb614f`).

## Done

- Phase 0 merged into `main` via PR #2. The acceptance line `make setup && make up && make test && make lint` passed on a clean state (47 tests, ruff and mypy clean).
- Phase 1 plan written and approved.
- Branch `phase-1-simulated-company` created.

## In progress

Nothing. Waiting for the go-ahead to implement Phase 1.

## Decisions made

### Phase 1 (from plan review)

1. **A fifth team, `platform`, owns all raw sources, staging and intermediate models.** The four business teams (sales, marketing, finance, product) own their marts and dashboards. Consequence: most Phase 3 root causes surface at sources and staging, so most incidents route to platform. That imbalance is realistic and is a finding Phase 3 should surface, not hide.
2. **Every dbt model is a full-refresh table.** Every node then has a real storage size and build cost, which the Phase 2 scan proxy and incremental-candidate recommendation need.
3. **`teams.yaml` gains a `members` list.** The registry rejects an owner who is not a member of the team on the same entry. Phase 2 reuses it as the person-to-team mapping for ad hoc users.
4. **Marts, abandoned models and exposures carry a team prefix** (`finance_fct_revenue_daily`). A team glob sets a default owner and narrower globs override it, so "most specific wins" has real work to do.
5. **Ownership ties fail the check.** When two different patterns match with equal specificity, `metadata check` reports ambiguous ownership instead of picking by file order.
6. **Freshness runs on simulated time.** A project macro overrides `duckdb__collect_freshness` so `snapshotted_at` is `var('simulated_now')`, not the wall clock. Otherwise every source reads as stale, because the data lives in January 2026.
7. **Raw data is generated in SQL from pure hashes**, never `random()`, and written in primary-key order. Row order drives compression, and compression drives the storage sizes Phase 2 prices.
8. **Abandoned models get no dbt tests, and every non-abandoned model feeds at least one exposure.** Both are enforced by tests, so Phase 2's unused list is exactly the abandoned set.

### Carried from Phase 0

- GNU make 4.4.1 via winget. Makefile recipes pinned to bash.
- `CLAUDE.md` stays at `docs/CLAUDE.md`.
- Placeholder CLI commands exit non-zero until their phase lands.
- `mypy` pinned `>=1.10,<1.20` and built from source because of this machine's Application Control policy. Still awaiting owner review.

## Verified against installed versions

dbt-core 1.12.5, dbt-duckdb 1.11.0, checked in `.venv`:

- `dbt` runs here. The Application Control policy that blocked mypy does not block it.
- `query-comment` supports `comment` and `append`.
- Generic test arguments must sit under an `arguments:` key (`require_generic_test_arguments_property` defaults to `True`).
- Source `freshness` and `loaded_at_field` belong under `config:`.
- `collect_freshness` is dispatched and overridable. `dbtRunner` is available for in-process invocation.

## Known issues

- `make demo` still fails, by design, until every phase lands.
- SQL Server has never been booted; first real use is Phase 4.
- `uv` prints a `VIRTUAL_ENV` warning if an outer virtualenv is active. Harmless.

## Open questions for the owner

Defaults in brackets. None blocks starting.

1. OK to change "4 teams" to "4 business teams plus a data platform team" in `docs/CLAUDE.md`? [yes]
2. Registry rules that match nothing: warn or fail? [warn]
3. Equal-specificity ownership matches: fail, or last rule in the file wins like CODEOWNERS? [fail]
4. Integration tests add about a minute to `make test`. One target, or split unit and all? [one target]
5. From Phase 0: accept the mypy `<1.20` cap? Add CI before Phase 5?

## Next step

Implement Phase 1 on `phase-1-simulated-company`, in this order, one commit each:

1. Platform team, team members, settings tier weights.
2. Deterministic raw data generator and `platform-ops seed`.
3. dbt skeleton: project, profile, sources, simulated-time freshness override, custom generic tests.
4. Staging and intermediate models.
5. Hand-written team marts and 12 exposures.
6. Codegen for mart rollups, output committed.
7. 12 abandoned models.
8. `platform-ops build` through `dbtRunner`, persisting lineage after the build.
9. Ownership registry, `ownership.yaml`, `metadata check`, `ops.node_ownership`.
10. Lineage graph, helpers, `ops.lineage_edges`.
11. Integration tests: structure on a `dbt parse` manifest, plus end-to-end build at scale 0.01.
12. ADRs 0002 to 0004, module READMEs with real numbers, final update here.

Phase 1 acceptance: `make seed && make build` succeeds, `platform-ops metadata check` passes, a test proves every abandoned model has zero downstream nodes and zero exposures, and the lineage helpers have unit tests on a hand-built graph.
