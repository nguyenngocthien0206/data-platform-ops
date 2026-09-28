# Phase 6 review record

A whole-repo review after five phases built one after another. Everything below was found by reading the code module by module, by a broader lint rule set, by a coverage report, and by the test suite itself. Each finding has a decision: fixed in this phase, accepted with a reason, or deferred to Phase 7.

The rule for the phase was no behaviour change: `make demo` had to write the same reports, byte for byte, before and after. It did.

## Method

1. **Baseline** on `main` (`93d74f4`): a clean `make demo` with a SHA-256 of every file in `reports/`, the test suite with per-test durations, and a coverage report (94% of statements in `src/`).
2. **Broader lint**: `RUF`, `PERF`, `PIE`, `RET`, `C4` and `PTH` added to the existing `E`, `F`, `I`, `UP`, `B`, `SIM`.
3. **Manual review** of `common`, `metadata`, `simulation`, `cost`, `incidents`, `reconcile`, `dashboard`, the CLI and the tests, looking for correctness, dead code (cross-checked against coverage), module boundaries, stale docstrings and missing tests.
4. **Verification** after the changes: the same clean `make demo`, report hashes compared with the baseline, `make readme-check`, and the full suite timed against `main` on the same machine in the same session.

## Findings

| # | Where | Kind | Finding | Decision |
|---|---|---|---|---|
| 1 | `incidents/report.py`, `reconcile/report.py` | boundary | Both imported the Markdown `table()` helper from `cost/report.py`, so two modules depended on the cost module's internals. | Fixed: `common/markdown.py`. The modules' own formatters (`pct`, `usd`, `hours`) stay where they are, because they format differently on purpose. |
| 2 | `simulation/migration_faults.py`, `reconcile/*` | boundary | `simulation` imported `reconcile` (`TruthCell`, `TABLES_BY_NAME`) while `reconcile` imported `simulation`: a package cycle. | Fixed: `TruthCell` lives with the fault injector; the injector takes the table keys as a parameter. |
| 3 | `incidents/lifecycle.py` | boundary | `stable_hash` came from `simulation.workload`, a shared determinism primitive living inside one module. | Fixed: `common/hashing.py`, with a test that pins its value. |
| 4 | tests | missing test | Nothing stopped boundary problems like 1 to 3 from coming back. | Fixed: `tests/test_architecture.py` parses every import (including those inside functions) against an allow-list. It fails on the old code for exactly findings 1 to 3. |
| 5 | `simulation/workload.py` | boundary | `simulation` uses the cost module's collection layer (`collect`, `growth`, `sizes`, `schema`). | Accepted: the simulated warehouse carries the cost module's instruments the way a real warehouse carries a query history. Separating them would be a large change for no behavioural gain. The architecture test names it as the one exception. |
| 6 | `cost/run.py` `_write_table` | correctness | Created the table outside the transaction and filled it inside, so a reader could see an empty table in between. | Fixed: `common/db.replace_table` recreates and fills in one transaction. |
| 7 | `cost`, `incidents`, `reconcile` | duplication | "Replace an `ops` table" was written three ways (`_write_table`, `_create` plus inserts, `_replace` via Arrow). | Fixed: `replace_table` and `replace_table_from_arrow` in `common/db.py`, with tests, including that a failed replacement leaves the old table. |
| 8 | `tests/conftest.py`, `scripts/make_vendor_fixtures.py` | duplication | Two copies of the "isolated config" writer. | Fixed: `common/sandbox.write_isolated_config`. The regenerated vendor fixtures are identical to the committed ones. |
| 9 | `common/clock.py` | dead code | `SystemClock` and the simulated clock's `step`, `tick`, `schedule`, `daily_at`, `reset` and `from_settings` were used only by their own tests. | Fixed: removed with their tests. The clock keeps `now`, `advance` and `advance_to`, which is what the code uses. |
| 10 | `common/config.py`, `cost/sizes.py`, `dashboard/*` | dead code | `get_settings()`, `sizes.latest()`, `charts.chart_spec()` and `Warehouse.available()` were never called. | Fixed: removed. |
| 11 | across `src/`, `tests/`, `scripts/`, `dashboards/` | lint | 123 `# fmt: skip` markers, 17 of which ruff ignores (RUF028); 8 `noqa` for rules that are not enabled; small findings from the broader rules (unused variable, `open()` over `Path.open()`, dict comprehensions, a needless assignment). | Fixed: markers removed and the code formatted; two tables where alignment carries meaning (the fault catalogue, the legacy name lists) use a proper `# fmt: off` block. The broader rules stay on. |
| 12 | `reconcile/run.py` | correctness | Connecting to a legacy engine caught every `Exception`, which would also hide programming errors as "not reachable". | Fixed: only the drivers' own errors and `OSError`. |
| 13 | `reconcile/canonical.py`, `cost/run.py` | typing | Three `type: ignore` comments. | Fixed: honest types (`Any` for raw database values, a `cast` to `ActorType`). |
| 14 | `common/clock.py`, package docstrings, `cost/collect.py`, `cli.py` | stale docs | "Six weeks" (the window has been 13 weeks since Phase 2); "Built in Phase N" boilerplate; a collector "coming in Phase 5" that exists; a CLI docstring describing placeholder commands that no longer exist. | Fixed: rewritten to describe the code as it is. |
| 15 | `cli.py` `dashboard` | correctness | The command looked for `dashboards/` next to the config file, so any config outside the repo could not find the app. Found by the new CLI test. | Fixed: the app is located from the repo, where it ships. |
| 16 | `incidents/notify.py`, `cli.py` | missing test | The Slack post and the `dashboard` command were covered only by `--help` and URL parsing. | Fixed: a local HTTP server on 127.0.0.1 receives the Slack post (and a refused connection is logged, not raised); a stubbed Streamlit process pins the launch arguments. |
| 17 | tests | test time | Three fixtures took most of the suite's time, and two of them built the same kind of warehouse separately. | Fixed: one session-scoped `full_warehouse`, built as `make demo` builds it, serves the dashboard tests and the first run of the cost and incident end-to-end tests. The independent second runs that prove determinism stay. |
| 18 | `tests/conftest.py` | correctness | The shared fixture first left its config in the environment for the rest of the session, which broke a later test. Introduced and caught within this phase. | Fixed: the environment is restored before the fixture returns. |
| 19 | repository | tooling | `core.autocrlf=true` turns LF-generated files into CRLF on checkout, so regenerated fixtures show phantom diffs. | Deferred to Phase 7: a `.gitattributes` with `eol=lf`. |
| 20 | environment | tooling | Windows Smart App Control, now enforcing on the development laptop, started blocking DuckDB 1.5.5 and uv's `pytest.exe` launcher. Blocking varied by file and over time (DuckDB 1.5.0 and 1.4.1 loaded, 1.5.5 and 1.4.4 did not). | Resolved by the owner, who switched Smart App Control off. The pandas and pyarrow caps added for it are revisited in Phase 7. |
| 21 | `make demo` | performance | The baseline `make demo` took 10 min 34 s on the development laptop, over the 10-minute budget; the verification run after the changes took 7 min 57 s. Phase 5 measured 6 min 54 s and 9 min 1 s. The spread comes from machine state, not from the code. | Deferred to Phase 7, whose scope includes restoring and measuring the margin. |
| 22 | tests | coverage | The SQL Server paths (about a third of `reconcile/connectors.py`) run only where the optional driver and the Compose profile are available. | Accepted: SQL Server is optional by design (ADR 0001); the golden-row and end-to-end tests cover it where it runs. |

## Before and after

| Measure | Before | After |
|---|---:|---:|
| Reports from a clean `make demo` (SHA-256, excluding `cost_proxy_accuracy.md`) | baseline | identical |
| `make readme-check` | 178 of 178 numbers found | 178 of 178 numbers found |
| Test suite on `main` and on this branch, same machine, same session | 9 min 50 s | 6 min 24 s |
| Tests | 294 passed, 3 skipped | 305 passed, 3 skipped |
| Ruff findings under the broader rule set | 36 | 0 |
| `# fmt: skip` markers | 123 | 0 (two `# fmt: off` blocks) |

`cost_proxy_accuracy.md` is left out of the byte comparison on purpose: it reports DuckDB profiler row counts, which depend on physical row order and are not guaranteed identical between runs (Phase 2).

The 5-minute target for `make test` was not reached. The remaining time is dominated by work the tests exist to do: the shared warehouse (a 3-week simulation, pricing, the incident scenario and the reconciliation), the independent incident run with a full dbt run and test added on four fault nights, and the independent cost run. Cutting further would mean dropping one of those checks, which the phase ruled out.
