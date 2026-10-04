# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-09-29

## Current phase

Phase 10: Ownership in the company's dbt CI. Implemented, waiting for owner review, then the `v1.1.0` tag.
Branch: `phase-10-ownership-ci` (off `main` at `1bb96d7`).

Phases 0 to 9 are done and released as 1.0.0. Phase 10 prepares 1.1.0.

## Done

- Phase 2 merged (PR #4). Reference numbers at scale 1.0: `make simulate` 280 to 290 s, `make cost` 20 to 24 s, byte-identical `cost.md` across two runs, unused list exactly the 12 abandoned models.
- Phase 3 implemented: fault injector with repair (`simulation/faults.py`), and `incidents/` with ingestion, grouping, severity, routing, notifier, lifecycle, the 21-day scenario, metrics and report. `platform-ops incidents run` is live; ADR 0008 and `src/platform_ops/incidents/README.md` written.
- Phase 3 reference numbers at scale 1.0: `make incidents` 2 min 22 s to 2 min 56 s over three runs, byte-identical `incidents.md` and postmortems across two runs. 8 faults, 18 failing checks, 8 incidents (exactly one per fault), 8 pages. Routing right person 8 of 8 (naive rule 3 of 8). dbt ran on 8 of 21 nights, 19 invocations.

- Phase 3 merged (PR #5).
- Phase 4 implemented: `reconcile/` (schema, canonical rules and renderers, connectors for Postgres, SQL Server and DuckDB, legacy generator, migration job with four defects, segmented diff, classification, metrics, sign-off report) and `simulation/migration_faults.py`. `platform-ops reconcile run` is live, so every Makefile target is implemented. ADR 0009 and `src/platform_ops/reconcile/README.md` written.
- Phase 4 merged (PR #6).
- Phase 5 implemented: Streamlit dashboards (`dashboards/`, data layer and charts in `src/platform_ops/dashboard/`), `platform-ops dashboard`, BigQuery and Snowflake collectors (`cost/collectors/`) with generated fixtures and contract tests, `make readme-check`, ADR 0010, the root README, a dashboards README and a collectors section in the cost README.
- Phase 5 merged (PR #7).
- Phases 6 and 7 added to `docs/SPEC.md` by the owner's decision: Phase 6 is a whole-repo review and cleanup, Phase 7 is hardening and release (1.0.0).
- Phase 6 merged (PR #8).
- Phase 7 implemented: the toolkit in a container (`Dockerfile`, `.dockerignore`), `app`, `dashboard` and `browser-check` services in Compose behind profiles, `docker-*` make targets next to the host ones (`demo` now runs `clean setup up pipeline`, and `pipeline` is every module in order), a headless Chromium check of every dashboard page (`scripts/check_dashboards.py`), `.gitattributes` with `eol=lf`, and the pandas and pyarrow caps lifted (pandas 3.0.6, pyarrow 25.0.1).
- Phase 7 acceptance, from a fresh clone of the branch plus the uncommitted Phase 7 files, with the working copy's `.env` (the Postgres volume already had its password): `make docker-build`, `make docker-demo`, `make docker-test`, `make docker-lint`, `make docker-readme-check` and `make docker-browser-check` all passed. Two clean container runs wrote byte-identical reports, identical to the native Windows run and to the Phase 6 baseline (excluding `cost_proxy_accuracy.md`). Tests in the container: 308 passed, 0 skipped (the SQL Server golden rows and e2e run there). `readme-check` 178 of 178. Browser check 8 of 8 page loads (4 pages, light and dark), screenshots reviewed.
- Phase 7 reference numbers, same laptop (16 CPUs, Docker Desktop with 14 GB), first on pandas 2.3.3 and pyarrow 21.0.0, then after the caps lift:
  - Image: first build with no cache 3 min 27 s, 1.58 GB on disk (364 MB compressed); rebuild after a code change 12 s; rebuild after a lock change 2 min 5 s.
  - `make docker-demo` from a clean state: 8 min 41 s (includes starting SQL Server). Per step, run 2: simulate 256 s, cost 16 s, incidents 189 s, reconcile 65 s (8 min 48 s). After the caps lift: 220 s, 12 s, 140 s, 56 s (7 min 16 s).
  - Native `make demo` on this branch: 8 min 2 s, and 7 min 49 s after the caps lift.
  - `make docker-test`: 7 min 48 s (pytest 7 min 40 s), 6 min 43 s after the caps lift (pytest 6 min 35 s). Native `make test` after the lift: 6 min 48 s, 305 passed and 3 skipped (no `pymssql` in the native venv).
  - `make docker-lint`: 4 min 32 s, 3 min 48 s after the lift. mypy dominates: it is built from source (`no-binary-package`) and starts without a cache in every fresh container.
  - `make docker-readme-check` 3 s; `make docker-browser-check` 30 s once both images exist (the first Playwright image pull took about 7 min).
  - Full-scale reconcile against Postgres and SQL Server in the container (`reconcile run --engine postgres --engine sqlserver`): 9 min 14 s, SQL Server about 8 of it. SQL Server as delivered: 94.984%, 99.516% and 34.836% row match, 100% recall and classification accuracy, not signed off, with the same classes and counts as Postgres.
- Phase 7 merged (PR #9).
- Phase 8 implemented:
  - mypy moved to its compiled 2.x wheels (1.19.1 to 2.3.1, which adds `ast-serialize`); the `<1.20` cap and `no-binary-package` are gone. mypy 2 reported nothing new under the strict config.
  - `.github/workflows/ci.yml` runs lint, the ownership check and the tests in the toolkit container with Postgres, on every pull request and push to `main`. The native `uv` job is gone.
  - `.github/workflows/sqlserver.yml` runs the full suite with Postgres and SQL Server, on demand, and fails on any skip.
  - `.github/workflows/release.yml` runs on a `v*` tag: it reuses `ci.yml`, checks that the tag equals the `pyproject.toml` version, pushes the amd64 image to `ghcr.io/nguyenngocthien0206/data-platform-ops` (`1.0.0`, `1.0`, `latest`), and creates a GitHub Release whose notes come from `CHANGELOG.md`.
  - Version 1.0.0: `pyproject.toml` is the only place it is written; `platform_ops.__version__` reads the installed package metadata. `CHANGELOG.md` has one 1.0.0 entry covering Phases 0 to 8. `scripts/release_notes.py` extracts a version's notes, and `tests/test_release.py` keeps the version, the package and the changelog in agreement.
  - Compose takes `TOOLKIT_IMAGE` to run a published image. The Makefile takes `DOCKER_ENGINES` (CI starts Postgres only) and has a new `docker-metadata-check` target.
- Phase 8 verification, before push: actionlint (1.7.12) found 0 errors in the three workflows. From a fresh clone of the branch plus the uncommitted files, the CI job's steps replayed locally (`docker build` instead of buildx with the gha cache) all passed. The SQL Server job's steps passed with nothing skipped. `make docker-demo` with `TOOLKIT_IMAGE` set to a GHCR-style tag of the image wrote reports byte-identical to the Phase 7 hashes. `platform-ops version` prints 1.0.0 natively and in the container.
- Phase 8 reference numbers, same laptop:
  - Cold `make lint`: native 5 min 13 s before, 1 min 2 s after (warm 4 s); `make docker-lint` 3 min 48 s in Phase 7, 62 s now.
  - CI job replay: image build 103 s (warm uv cache), lint 62 s, ownership check 13 s, tests against Postgres 6 min 37 s (309 passed, the 3 SQL Server cases skipped with their reason).
  - SQL Server job replay: 7 min 2 s, 312 passed, 0 skipped.
  - `make docker-clean docker-demo` with `TOOLKIT_IMAGE`: 7 min 8 s.
- Phase 8 merged (PR #10). CI passed on the pull request in about 6 min 20 s on `ubuntu-latest`: image build 97 s, lint 44 s, ownership check 7 s, tests against Postgres 3 min 31 s (faster than the 16-core laptop).
- Phase 9 implemented:
  - Root `README.md`: a "Run it in Docker" section (build or `TOOLKIT_IMAGE`, `make docker-demo`, the dashboards, `docker-readme-check`, `docker-test`, `docker-browser-check`, `docker-clean`), the runtime line updated to the latest runs, the ADR table extended to 0013 with a link to the index, and the repository layout updated. The sentence claiming CI runs without Docker is gone.
  - ADRs 0011 (module boundaries pinned by a test, and the review before the release), 0012 (the toolkit in a container) and 0013 (CI and release from the same container), and `docs/adr/README.md`, the index of all 13. Later notes on ADR 0007 and 0008 (the lifted budget, the designs kept) and 0010 (the caps and mypy, the container, internal use). A follow-up in `docs/review/phase-6-review.md` closes the three findings deferred to Phase 7.
  - `docs/SPEC.md`: a status line, the repository layout with the Dockerfile, CHANGELOG, workflows and review record, and the Makefile table with `pipeline`, `fmt`, `clean` and every `docker-*` target.
  - `dashboards/README.md`: the container command, the browser check, and the three presentation problems stated as known limits.
- Phase 9 acceptance, from a fresh clone of the branch plus the uncommitted files, with the working copy's `.env`: following the README's Docker section, `make docker-clean`, `docker-build` (31 s with the dependency layer cached), `docker-demo` (7 min 20 s), `docker-readme-check` (178 of 178) and `docker-browser-check` (8 of 8 page loads) passed, and the reports were byte-identical to the Phase 7 baseline. `make docker-dashboard` answered on `localhost:8501` within 6 s. All 44 relative links in the 27 Markdown files resolve (a one-off local script, not committed). All 13 ADRs are in the index.
- Phase 9 merged (PR #11).
- **1.0.0 released.** The owner pushed `v1.0.0` from `main` after the Phase 9 merge. The release workflow passed (CI rerun, tag checked against the version, image pushed, GitHub Release "data-platform-ops 1.0.0" with the changelog notes). The GHCR package is public, with tags `1.0.0`, `1.0` and `latest`. `sqlserver.yml` passed on `main`. Phase 8 and Phase 9 acceptance are closed.
- Published image check (2026-10-04): an anonymous `docker pull ghcr.io/nguyenngocthien0206/data-platform-ops:1.0.0` took 54 s (amd64, OCI labels with version 1.0.0 and the repository source); `platform-ops version` prints 1.0.0; `TOOLKIT_IMAGE=ghcr.io/nguyenngocthien0206/data-platform-ops:1.0.0 make docker-clean docker-demo` took 6 min 16 s and wrote reports byte-identical to the Phase 7 baseline.
- Phase 10 implemented:
  - `platform-ops metadata check --manifest ... --registry-dir ...` runs on any dbt project and adapter: no settings, no database, nothing written but an optional `--summary-json`. Findings name the file to change and how (a ready-to-paste rule for an unowned dataset); `--format github` adds annotations on the dataset's file or the rule's line; `--report-only` never fails; exit 1 means owners to fix, 2 means the check cannot run. New module `metadata/output.py`; `check.py` records each finding with its node, rule and fix.
  - Seeds and snapshots count as owned resources. `teams.yaml` takes an optional `telegram_chat`.
  - ClickHouse: a `clickhouse` Compose profile (`clickhouse/clickhouse-server:26.8.16.41`), a `dbt-clickhouse` extra (1.10.3) in the image, `DOCKER_ENGINES` defaulting to all three engines, CI running Postgres and ClickHouse.
  - `tests/fixtures/dbt_clickhouse` (`acme_analytics`): 15 models in a layout unlike the demo's, a local package, 4 seeds, a snapshot, 3 sources, 2 exposures and its own `ownership/` registry. `tests/test_ownership_external.py` parses it, checks every finding type, exit code and annotation, and builds it for real on ClickHouse.
  - `examples/github-actions/ownership-check.yml` for a company dbt repository, a section on it in the metadata README, ADR 0014, version 1.1.0 and its CHANGELOG section.
- Phase 10 verification, from a fresh clone of the branch plus the uncommitted files: `make docker-build` 104 s, `make docker-lint` 61 s, `make docker-metadata-check` 12 s (the demo still 9 sources, 118 models, 12 exposures, 139 rows), `make docker-test` with Postgres, ClickHouse and SQL Server 7 min 4 s, 327 passed and nothing skipped. `make docker-clean docker-demo` 6 min 58 s, reports byte-identical to the Phase 7 baseline. The example's `docker run` line, run from the built image as another user on a read-only mount of the parsed fixture: exit 0 on its registry, exit 1 with a repository-relative annotation when a rule is removed, exit 0 with `--report-only` (summary 24 of 25 owned, 0.96). actionlint found nothing in the example and the three workflows. Native `make lint` 3 s warm.
- Phase 10 merged (PR #12).
- Fix after Phase 10 (branch `fix-make-shell-on-windows`): `make` run from PowerShell or cmd failed with `! was unexpected at this time.`, because `SHELL := /usr/bin/env bash` only resolves inside Git Bash, so GNU make on Windows fell back to cmd.exe (the `bash.exe` on PATH was WSL's). On Windows the Makefile now runs recipes with Git for Windows' bash (`GIT_BASH`, default `C:/Program Files/Git/bin/bash.exe`, overridable). Verified from PowerShell (`check-env`, `help`, `make -n docker-demo`, a real `make docker-build`) and from Git Bash; Git's bash resolves Git's `find`, `grep` and `awk` and Docker Desktop's `docker`. The README states the Windows requirement.
- Phase 6 implemented: broader lint rules on and clean; `# fmt: skip` noise removed; module boundaries fixed and pinned by `tests/test_architecture.py`; shared helpers in `common` (`markdown.table`, `db.replace_table`, `hashing.stable_hash`, `sandbox.write_isolated_config`); dead code removed; stale docstrings rewritten; new tests for the Slack post and the dashboard command; a shared `full_warehouse` test fixture. 22 findings recorded with decisions in `docs/review/phase-6-review.md`.
- Phase 6 acceptance: a clean `make demo` wrote byte-identical reports to the baseline (excluding `cost_proxy_accuracy.md`, non-deterministic by design); `make readme-check` 178 of 178; test suite 9 min 50 s on `main` against 6 min 24 s on the branch, same machine and session (305 passed, 3 skipped). The 5-minute target was not reached; the floor is the independent runs the determinism tests need.
- Phase 5 acceptance, from a fresh clone of the branch: `make setup && make up && make demo && make readme-check` passed in 7 min 41 s with byte-identical reports; the dashboards served every page.
- Phase 5 reference numbers: `make demo` from a clean state took 6 min 54 s and 9 min 1 s in two runs on the same laptop (slower machine state on the second: Docker Desktop just started, Smart App Control checking native modules), with identical reports. `make readme-check`: 178 numbers across the root and module READMEs, all found in the reports.
- Phase 4 reference numbers at scale 1.0: `make reconcile` about 52 s, byte-identical `reconciliation.md` across two runs. As delivered: 199,465 planted discrepancies, 100% recall, precision and classification accuracy; not signed off. Job fixed: 44 planted, all found; the diff moves 0.6% to 9.6% of the rows a naive comparison would. `make demo` from a clean state: 6 min 54 s.

## In progress

Nothing. Phase 10 is waiting for the owner's review.

## Decisions made

### Phase 10 (owner, at planning)

1. **The consuming CI runs `dbt parse`** and hands the manifest to the toolkit, which needs no adapter.
2. **A report-only mode**, to measure the baseline and switch the check on without blocking merges.
3. **ClickHouse runs in PR CI**, next to Postgres.
4. **Release 1.1.0** at the end of the phase; the owner pushes the tag.

### Phase 10, made during implementation

50. **`dbt parse` with `dbt-clickhouse` runs without a server**, verified with nothing listening on the port. A parse-only profile selected with `--profile` is enough, so the example needs no secret.
51. **`--repo-root` instead of a path prefix.** Annotation paths are made relative to the repository root, which works wherever the project and the registry sit inside it; a file outside it keeps its full path.
52. **An installed package's datasets get no file annotation**, because their files are not in the repository; the finding still lists the rule to add.
53. **Exposures appear in `run_results.json` as `no-op`** in a dbt build; the integration test allows that status for them.
54. **A package macro is called with its namespace** (`acme_shared.cents_to_amount`); dbt does not resolve another package's macros unqualified.
55. **Compose defaults for ClickHouse credentials** (`:-`, not `:?`), matching the fixture profile's defaults, so an existing `.env` without the new keys keeps working.

### Before Phase 10 (owner)

1. **The toolkit stays public; company configuration lives in the company's repositories.** `ownership.yaml` and `teams.yaml` for company projects sit in each dbt repository, and CI pulls the public image and points it at them. Names, channels and chats of the company never enter this repository. Added to the SPEC's Phase 10.
2. **The company's dbt repositories run CI on GitHub Actions**, so the Phase 10 example is a GitHub Actions workflow.
3. **Company policy allows the public image in company CI** (the owner asked). The example workflow pulls `ghcr.io/nguyenngocthien0206/data-platform-ops` at a fixed version.
4. **The pilot is the data platform team.** It owns the shared layers (staging, intermediate), understands ownership best and will maintain the rules, so the first rollout meets the least friction.
5. **The pilot's dbt project runs on ClickHouse** (`dbt-clickhouse`), self-managed by the company. First chosen as BigQuery, then changed by the owner: there is no BigQuery environment to test against, while ClickHouse runs locally in Docker, so the fixture project, the local tests and the pilot use the same engine.
6. **The fixture project is built here**: a small dbt project on the ClickHouse adapter with a layout unlike the bundled one (several model folders, a package, sources and exposures), run against a ClickHouse service behind a Compose profile so its artifacts are real. No company data or manifest is needed.
7. **Self-managed ClickHouse has no vendor bill.** Cost for it (Phase 12, if ClickHouse is the first engine) means sharing the infrastructure cost by usage, so the SPEC's Phase 12 acceptance now covers that case.

### After Phase 8: internal use (owner)

1. **The toolkit goes to the teams inside the company, not open source.** Open source would mean supporting many engines and outside users. The company's stack: Snowflake, BigQuery or ClickHouse; dbt run from Airflow and from Dagster; alerts to Slack or Telegram; no migration under way.
2. **One module per phase, lowest integration cost first**: Phase 10 ownership in the company's dbt CI (no warehouse access, and the other modules route on it), Phase 11 incidents on real dbt runs (engine-agnostic, since dbt artifacts look the same on every adapter, and the pain is daily), Phase 12 cost for the one engine with the largest bill (collectors are engine-specific and need read access and a data handling review). Reconciliation waits for a real migration.
3. **Phase 9 is trimmed** to what 1.0.0 needs: a Docker section in the README, ADRs for Phases 6 to 8 with an index, notes on the ADRs whose context changed, the SPEC's Makefile table, and fixes to anything stated wrongly. The Docker-first rewrite and the docs check in CI are dropped.
4. **Shared state for company runs lives in one Postgres database**, the same whatever the warehouse, rather than in each warehouse.
5. **Airflow and Dagster integrations live in this repo as optional extras** (`platform-ops[airflow]`, `platform-ops[dagster]`), thin layers over the same CLI and Python API.

`docs/CLAUDE.md` was updated to match: the demo and tests stay offline and deterministic; code for company systems is optional, sits behind the existing interfaces and reads credentials only from the environment; no orchestrator inside the toolkit.

### Phase 8 (owner, at planning)

1. **The image is amd64 only**, like the SQL Server image. Apple Silicon runs it under emulation or builds it locally with `make docker-build`.
2. **The on-demand job runs only the SQL Server tests** (the full suite, nothing may skip), from `workflow_dispatch`. A demo and `readme-check` in CI belong to Phase 9.
3. **The release reruns the full CI first**, then publishes only if the tag equals the version and the changelog has notes for it.

Planned defaults: CI calls the same `docker-*` make targets as a laptop, with Postgres from Compose; CI writes `.env` from `.env.example` on fresh volumes; the native `uv` CI job is dropped; the browser check stays out of CI.

### Phase 8, made during implementation

45. **The version is read from the package metadata**, so `pyproject.toml` is the only place it is written, and `uv lock` records it.
46. **Release notes skip Keep a Changelog's link definitions** at the end of the file, and a missing section exits non-zero so a tag without notes never publishes.
47. **The SQL Server job runs its step with `shell: bash`**, which gives `pipefail`, so `make docker-test | tee` still fails when the tests fail; a grep on the summary fails the job on any skip.
48. **`latest` comes from `docker/metadata-action`'s default**, which adds it for stable semver tags only; the workflow lists the `{{version}}` and `{{major}}.{{minor}}` patterns.
49. **No `.mypy_cache` in CI.** With compiled wheels a cold `make docker-lint` takes about a minute, so a cache is not worth its keys.

### After Phase 7: constraints that no longer apply (owner)

Smart App Control is off and the 10-minute budget is lifted, so what was built around them was reviewed once more.

1. **mypy's cap and source build go in Phase 8** (added to the SPEC). They were the last Smart App Control workaround in the tooling, and they make `make docker-lint` take about 4 minutes, which CI would pay on every run. `python -m mypy` stays.
2. **The budget-driven designs stay as they are.** The real dbt build every 28 days with daily replay (ADR 0007) and the incident scenario that runs dbt only on nights a fault is active (ADR 0008) are documented. Their reports match across the native run, the container and the Phase 6 baseline. Changing them would redo every reference number and README figure for realism alone. The pandas and pyarrow caps were already lifted in Phase 7.

### Phase 7 (owner, at planning)

1. **The pandas and pyarrow caps are lifted only after the container is verified on the current lock**, in their own commit, and kept only if the reports stay byte-identical and everything passes. They did, in the container and natively, so the lift stays.
2. **The dashboards are checked in a real browser automatically**: Playwright and headless Chromium in their own image (`browser-check`), against the dashboards served from the toolkit container, with screenshots for a human look.

Planned defaults: work continues on `phase-7-containerized-rerun`; the code is baked into the image and generated state lives on named volumes, with only `reports/` bind-mounted; `.env` never enters the image; host targets keep working; the Docker section of the README waits for Phase 9.

### Phase 7, made during implementation

38. **Service addresses come from the environment, no code change.** The connectors already read `POSTGRES_HOST`, `POSTGRES_PORT`, `MSSQL_HOST` and `MSSQL_PORT`, and an environment variable wins over `.env`; Compose sets them to the service names and container ports. Streamlit's `localhost` binding in `.streamlit/config.toml` is overridden by `STREAMLIT_SERVER_ADDRESS=0.0.0.0` in the container.
39. **`make clean` empties the generated directories instead of removing them**, because inside the container they are volume mount points. On the host the effect is the same.
40. **The Playwright image ships the browsers but not the Python package**, so `browser-check` is built from it with `playwright` pinned to the image's version (an inline Dockerfile in Compose, context `scripts/`).
41. **The browser check waits on Streamlit's own script state** (`data-test-script-state="notRunning"` on the app root), then on every Vega chart having drawn. Watching the status widget let the first page pass before it had rendered. On a deep link such as `/cost` the frontend probes `/cost/_stcore/health` and `host-config`, gets a 404 and falls back to the root; those two probes are expected and ignored, and every other failed request fails the check.
42. **Screenshots grow the viewport to the content.** Streamlit scrolls an inner container, so a "full page" screenshot stops at the viewport.
43. **pandas 3 no longer installs `tzdata` on Linux** (only on Windows). The image has Debian's system time zone data, which `zoneinfo` reads first anyway, so nothing changed; the reconciliation's daylight saving cases still match.
44. **DuckDB's `icu`, `json` and `parquet` extensions are built into the wheel**, so nothing is downloaded at run time in the container either.

### Phases 7 to 9 (owner, after Phase 6)

1. **The project now has ten phases (0 to 9).** Phase 7 packages the toolkit in a container and reruns and verifies everything through Docker Compose; Phase 8 is CI and release; Phase 9 is all documentation. This replaces the earlier single "hardening and release" Phase 7.
2. **No time budget for `make demo` any more.** The 10-minute limit is lifted (`docs/CLAUDE.md` and the SPEC updated); measured times are recorded instead.
3. **Release in Phase 8:** CI runs checks on every pull request and push to `main`; a pushed `v*` tag builds the image, pushes it to GitHub Container Registry, and creates a GitHub Release from `CHANGELOG.md`. Version 1.0.0 and the changelog belong to Phase 8. The owner creates and pushes the tag.

### Phases 6 and 7 (owner, at planning, superseded above for Phase 7)

1. **Two more phases**: Phase 6 whole-repo review and cleanup, then Phase 7 hardening and release, so the release is cut on the cleaned code.
2. **Release**: `CHANGELOG.md`, version 1.0.0, and the tag commands; the owner creates and pushes the tag and the GitHub release.
3. **CI gets a Postgres job** (service container) in Phase 7; SQL Server stays optional and skipped in CI.
4. **Phase 6 rule: no behaviour change.** `make demo` reports must be byte-identical before and after; a bug whose fix would change a reported number is raised with the owner, not fixed silently.

Planned defaults for Phase 6: the review record is committed as `docs/review/phase-6-review.md`; the broader ruff rules stay on; `make test` target under 5 minutes, or the measured floor is reported.

### Phase 5 (owner, at planning)

1. **Dashboard tests run on a real warehouse at scale 0.01**: the ops-only data layer is tested on its own, and an AppTest smoke test renders every page against a warehouse built by all four modules (about 3 more minutes of `make test`).
2. **`make readme-check`** verifies that every number in the README "Results" sections appears in `reports/*.md`. READMEs stay hand-written; the check runs after `make demo`, not in CI.
3. **Vendor collector fixtures are generated from the simulated workload**: a deterministic script maps a sample of `ops.query_log` into BigQuery `INFORMATION_SCHEMA.JOBS` and Snowflake `QUERY_HISTORY` columns; the files are committed.

Planned defaults: no lineage graph on the Overview page; `make demo` does not run `readme-check`; `QueryRecord` gains an optional `bytes_scanned` for vendor-measured bytes; Streamlit usage stats off (offline after setup).

### Phase 5, made during implementation

30. **pandas capped `<3`, pyarrow `<22`** (asked the owner). Windows Smart App Control switched to enforcing on the development laptop and refused pandas 3.0.6 and pyarrow 25.0.1 (too new to have reputation); pandas 2.3.3 and pyarrow 21.0.0 load. Without the cap `make reconcile` and the dashboards do not run on that machine. Raise the caps once newer wheels load.
31. **`ops.query_log.bytes_scanned`** (nullable) stores vendor-measured bytes from a cloud collector; local collectors leave it NULL and `cost.md` is unchanged. Three tests that insert into `query_log` by position gained a NULL.
32. **The SQL parser ignores a comment after the final `;`.** Found by the collector contract tests: a warehouse records a dbt model's statement with dbt's query comment after the compiled SQL's semicolon, which parsed as a second statement.
33. **Fixtures carry the dbt query comment and modelled times.** The query log keeps dbt's compiled SQL from `target/run`, written before the comment is appended, so the fixture generator appends it as the warehouse would see it; elapsed times are modelled, never measured, so the fixtures are deterministic.
34. **README runtimes are marked `<!-- readme-check: runtime -->`.** Timings are printed on the console, not written to a report; every other results number must appear in the reports. Derived percentages in the READMEs were rewritten with the reports' own figures.
35. **Chart colours**: the validated reference palette, checked against Streamlit's own light and dark surfaces; three light-mode slots are below 3:1 contrast, so every chart has tooltips and its data table beside it.
37. **Streamlit always runs headless; `platform-ops dashboard` opens the browser itself** once the health check answers. Found by the fresh-clone run: on a machine that never ran Streamlit, a non-headless start stops at an interactive "Email:" prompt, so `make dashboard` hung.
36. **Hotspots can be empty at small scale** (tables under 20 MB never qualify); the Cost page says so instead of showing an empty table.

### Phase 4 (owner)

1. **PyIceberg plans the files, DuckDB reads them with `read_parquet`.** Offline; the DuckDB `iceberg` extension would need a download.
2. **`reconcile run` exits 0 whatever the verdict**; the verdict is in the report. `--strict` exits non-zero when the delivered migration is not signed off.
3. **`pymssql` as an optional extra** (`uv sync --extra sqlserver`). It carries its own driver; pyodbc needs ODBC Driver 18 on the host.
4. **Realistic time zone defect kept**: daylight saving ignored, about 65% of payments shifted by one hour.
5. **Canonical defaults**: Postgres neither trims nor folds case; SQL Server folds case, like its case-insensitive collation.
6. **Two passes** (asked during implementation): the job as delivered, then the job with its defects fixed. Systematic defects make every segment differ, so only the second pass shows what the segmented diff saves.

### Phase 4, made during implementation

24. **Legacy data has its own generator covering calendar 2025.** The Phase 1 data spans only a winter, so a daylight saving defect would never fire. Local times fall between 06:00 and midnight, avoiding the ambiguous hour when clocks go back.
25. **Hash: MD5 of the UTF-8 row string, split into two unsigned 32-bit halves, summed per segment.** Verified identical in Python, DuckDB 1.5.5, Postgres 16.15 and SQL Server 2022 CU27 on golden rows. SQL Server's legacy database uses a `_UTF8` collation so VARCHAR bytes match; style 126 drops a zero fraction, so microseconds are formatted by hand; pymssql mangles non-ASCII VARCHAR, so leaf values are fetched as NVARCHAR.
26. **Each side hashes its rows once into a temp table**, and every level groups that. The diff went from 53 s to 20 s at scale 1.0.
27. **The legacy export is read once for both passes.**
28. **Report percentages use three decimals and never round up to 100%.**
29. **Isolated test configs copy `.env`**, so the Postgres and SQL Server tests find their credentials; they skip when the engine is not reachable, so CI stays green without Docker.

### Phase 3 (owner, at planning)

1. **Run only what faults touch.** 3-week scenario, 8 faults covering all 6 types. dbt runs for real only on days a fault is active. Green days run nothing, because the baseline is all green; an e2e test proves a targeted run finds the same failures as a full one. (Selector changed during implementation, see 20.)
2. **Staging roots page the source owner**, because staging only renames and casts. Routing accuracy is reported per person and per team.
3. **`make demo` drops its redundant `seed` and `build` steps** (about 52 s); `make simulate` already does both.

Verified against dbt-core 1.12.5: `dbt build` skips everything downstream of a failed test (`Fail` is in `task/build.py` `MARK_DEPENDENT_ERRORS_STATUSES`), which would hide the alert storm. `dbt run` then `dbt test` skip only on `Error`, so the scenario uses those. Severity inputs (tier-weighted downstream consumers): customers 213, orders 189, order_items and products 160, campaigns and web_sessions 76, support_tickets 34, marketing_spend 20, payments 19.

### Phase 3, made during implementation

17. **A test that reads two models is its own subject.** A relationships test is attached to one model but depends on two. Treating it as its own node below both keeps a volume drop in orders from opening a second incident on order items.
18. **Open questions resolved with the planned defaults**: a recurrence after resolution opens a new incident linked to the old by root; appends send an update, not a page; severity thresholds stay in config.
19. **Repairs regenerate data from the seed**, no backup tables. The generator is deterministic, so "put back what the generator says" is exact; a round-trip test per fault proves every table matches a clean load.
20. **Selection is `@source:raw.<table>`, not `source:raw.<table>+`.** Every model is a table, so downstream-only runs compared fresh payments with a stale `stg_orders` and opened incidents nobody caused. `@` also rebuilds the parents of everything selected. Targeted equals full on all four fault nights.
21. **One parse, reused by every dbt invocation**, including freshness: dbt resolves `simulated_now` at run time, so freshness costs 0.6 s instead of 5.2 s.
22. **Faults land in pairs** on days 2, 5, 9 and 14, on different tables. With the freshness change this took `make incidents` from 3 min 13 s to 2 min 22 to 2 min 56 s.
23. **`dbt run` and `dbt test` are separate invocations with console logging off** in the scenario. Failing tests are expected; dbt's log file keeps the detail.

### Phase 2 (owner)

1. **Logical bytes, not stored bytes** (ADR 0005). Compressed size is not stable between identical runs.
2. **Modelled compute time**: 150 ms per query plus bytes at 200 MB/s, declared in settings and printed in the report. Wall-clock time is recorded, never priced.
3. **Real simulated dollars**, no projection factor.
4. **Products and customers change in place** during the window, so the incremental recommendation has sources to rule out.
5. **One warehouse per workload** (transform, BI, ad hoc) for compute pricing.
6. **BigQuery's 10 MB minimum per table** in scan pricing.
7. **13-week window** (not the SPEC's 6), so the 90-day unused check covers a real 90 days.
8. **Real dbt build every 28 days**, replayed daily in between. Weekly real builds measured at 8 to 9 minutes for `make simulate`, which breaks the 10-minute demo budget.

### Made during implementation

9. **History counts are independent of window length.** `raw_data.HISTORY_COUNTS` is the size by the window start; the id space is extended to cover the window. Otherwise stretching the window to 13 weeks would have shrunk the history.
10. **dbt builds no longer receive `simulated_now`.** Only source freshness uses it, and changing vars forces dbt to re-parse the whole project. With constant vars the saved parse is reused (full parse 9 s, then about 1 s).
11. **Row-count test bounds cover the whole window** (orders up to 680k at scale 1.0). The old upper bound failed mid-window as orders grew; the lower bound, which catches a volume drop, is unchanged.
12. **Payments are never loaded before their order.** Found by comparing seven daily appends with one seed at full scale: a payment stamped minutes before its order was lost when a load boundary fell between them. Tests now check the invariant and many uneven appends.
13. **Daily loads only generate ids that can land in the window**, about 0.4 s a day at scale 1.0 instead of 1.1 s.
14. **Writes batch into one transaction per build interval, and inserts use multi-row `VALUES`.** Measured on this laptop: each commit costs 0.3 to 0.9 s; for 400 rows, `executemany` 0.17 s, Arrow about 0.8 s (a fixed cost per call), multi-row `VALUES` 0.035 s.
15. **The proxy-accuracy report is a separate file.** It uses profiler row counts, which DuckDB does not guarantee to be identical between runs, so it must never affect `cost.md`.
16. **dbt threads 8** (from 4): 26.6 s against 30.0 s per `make build`.

### Earlier phases (still in force)

- Platform team owns sources, staging and intermediate; business teams own marts and exposures; most specific ownership rule wins, ties fail.
- Every model is a full-refresh table; abandoned models keep their team's default tier.
- `run_dbt` releases dbt-duckdb's cached DuckDB handle after every invocation.
- Freshness on simulated time; off for `products` and `marketing_campaigns`.
- GNU make via winget; mypy on its compiled 2.x wheels since Phase 8, still run as `python -m mypy`; CI runs lint, tests and `metadata check --parse` in the container.

## Known issues

Kept on purpose (design limits, documented in the ADRs):

- Replayed days price model reads on model sizes up to 4 weeks old, and dashboards read marts up to 4 weeks stale. Raw data is always current. Acceptable for quarterly cost attribution (ADR 0007).
- The bytes estimate ignores row-group pruning, so filtered queries are overestimated; ad hoc queries by more than twice what DuckDB scanned, measured per run in `reports/cost_proxy_accuracy.md`. That file is not deterministic by design (profiler row counts depend on physical row order), so it is excluded when reports are compared byte for byte.
- Filters written against a CTE or subquery column outside it are not traced to the base table for hotspot detection. The workload and dbt do not write filters that way.
- The alert storm is modest: 18 failing checks for 8 faults. The volume drop is the one fault with a real cascade (5 checks).
- Grouping is per run. Two unrelated faults failing the same downstream model in one run attach it to one of them by tie-break (ADR 0008).

Found by the Phase 7 browser check, stated as known limits in `dashboards/README.md` and ADR 0012 (presentation only; reports are unaffected):

- Cost page, "By month": the x axis repeats month labels (two ticks per month), and the last point is April, of which the 13-week window from 5 January covers only a few days, so every line drops at the end without saying why.
- Incidents page, "Timeline": every x-axis tick reads "12 PM"; the dates are only in the tooltips.
- Overview "Ownership" and Incidents "Nights dbt ran": narrow columns cut off the last table column (coverage, failing checks); it is reachable by scrolling the table.

Kept on purpose since Phases 8 and 9:

- `make docker-browser-check` and `readme-check` stay out of CI (the docs check in CI was dropped from Phase 9).
- SQL Server regressions are caught only when someone runs `sqlserver.yml`; run it before each release.

## Open questions for the owner

None open.

## Next step

The owner reviews and merges Phase 10 (`phase-10-ownership-ci`); CI on the pull request now runs ClickHouse. Then tag from `main`:

```bash
git switch main && git pull
git tag -a v1.1.0 -m "data-platform-ops 1.1.0"
git push origin v1.1.0
```

After the release: copy `examples/github-actions/ownership-check.yml` into the pilot repository with the registry in `ownership/`, run it with `REPORT_ONLY: "true"`, record the baseline from the `ownership-summary` artifact, complete the registry, then set `REPORT_ONLY: "false"` and make the job required. Then Phase 11 (incidents on real dbt runs), when the owner says to start.
