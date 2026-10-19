# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-10-15

## Current phase

Version 2.0.0 is ready to release. Phase 15 (documentation and the release) is implemented and verified on branch `phase-15-documentation` (off `main` at `3b8b38d`): 15a and 15b committed, 15c uncommitted, waiting for the owner's review. The work was split into three sessions (15a, 15b, 15c), one branch and one pull request for the phase. Phases 11a to 14 are merged (PRs #15 to #23). No phase is planned after 15.

Phases 0 to 9 were released as 1.0.0, Phase 10 as 1.1.0 and the registry bootstrap as 1.2.0. Version 2.0 (Phases 11 to 15) rebuilt the simulated company on Postgres and ClickHouse. This file was closed out for 2.0.0 in Phase 15c: the per-phase implementation notes of Phases 11 to 15 are in the git history of this file; what follows is what stands.

## Done

### Version 2.0.0, in short

- **The company** (Phases 11a, 11b): a legacy Postgres with a year of history and 12 hand-written reports; an incremental copy into ClickHouse by an `updated_at` watermark into `ReplacingMergeTree` raw; the dbt project on ClickHouse (118 models, a seed, a snapshot, 12 exposures); a ClickHouse user per team and service; Metabase dashboards generated from the exposures. A 16-week window: seven weeks before the migration, a six-week parallel run, three weeks of production.
- **reconcile** (Phase 12): the parallel run, 9 tables and 12 reports compared every night by a segmented checksum diff, report differences explained through dbt's lineage, a cutover after 14 clean nights.
- **cost** (Phase 13a): every query tagged; the stated bill shared out exactly by bytes read and stored; production to the owner, reads to the reader; the business case; BigQuery and Snowflake on the same measured workload. **dashboards** (13b): the Streamlit pages on the ops store, as their own read-only role.
- **incidents** (Phase 14a): faults in operation, every dbt invocation ingested into a Postgres store any run can feed, one incident per root across a run's invocations, routing to the feed's owner, resolution by a later healthy run, Slack and Telegram. **Orchestrators** (14b): Airflow 3 with Cosmos and Dagster with dagster-dbt in their own images and companies, calling the toolkit's CLI through standard-library hooks; the release waits for a fault to go through both.
- **Documentation** (Phase 15): the README around version 2.0 with an architecture diagram generated from the project and screenshots from the release run, how each problem was solved, an "Adopting it" section in every module README, the ADRs retired and renumbered (0001 to 0017), the SPEC's final layout and Makefile tables, a link check over every Markdown file, `readme-check` for 2.0, `CHANGELOG.md` 2.0.0, version 2.0.0.

### Phase 15c (uncommitted)

- **The release run**, in the container on the development laptop, at scale 1.0:
  - `make docker-demo` twice from a clean state: 52 min 30 s and 51 min 57 s. Both seeded 3,507,804 rows, added 2,427,173 and changed 4,259 over 112 days, ingested 15,521,421 rows and ran 65 dbt builds; 141 datasets owned, 376 lineage edges.
  - Fingerprints: 195 of 196 relations and grants identical (the one difference is `ops.company_run`, the run's random id); `reconciliation.md`, `cost.md`, `incidents.md` and both postmortems byte-identical between the runs, and identical to the 14a runs.
  - `make docker-metabase-check`: 12 dashboards, 69 cards, 23,717 rows, no error, in 124 s.
  - `make docker-test-orchestrators`: both passed in 1,019 s. Airflow: the faulted run failed in 152 s with 70 invocations, the run after the fix succeeded in 364 s, the check took 520 s. Dagster: 63 s with 2 invocations, then 93 s, the check 160 s. One incident at `stg_orders`, one page, one resolution each time.
  - `make docker-docs-images`: 8 of 8 dashboard page loads passed; 11 screens, 1.33 MB, committed in `docs/images/screens/`.
- **The README's results from that run.** `readme-check` first found 17 numbers not in the reports: the reconcile numbers in the root and reconcile READMEs were still Phase 12's (351 trimmed emails, 3,576,917 planted, 378,892,260 rows naive, 39,488,818, 79.822%, 12.577%), while the reports have said 349, 3,576,911, 378,885,458, 39,488,792, 79.721% and 12.578% since Phase 14; corrected. The cost README's unused-tables total (a sum the report does not print) now cites the largest table's $6.47; its incremental bytes and the root README's console numbers carry the new `console` marker; the freshness rule reads "two days". `make readme-check` now passes: 192 numbers checked across four READMEs, none missing.
- **`readme-check` for 2.0**: a second marker, `<!-- readme-check: console -->`, for a line whose numbers a command printed or the database held, each recorded here; tested.
- **Version 2.0.0**: `pyproject.toml` and `uv.lock` (only the project's version line changed), `CHANGELOG.md` 2.0.0 (added, changed, removed, upgrading from 1.x), the Actions example on the 2.0.0 image.
- **Runtimes in the READMEs** from the release run; the Dagster build after the fix took 93 s, so "about a minute and a half", not "under".
- **CI caught a diagram that differed between Windows and Linux**: `postgresql.svg` was downloaded with CRLF line ends, git stored it with LF (`* text=auto eol=lf`), but the working copy kept CRLF, so the diagram drawn on the development laptop embedded different bytes than CI's checkout. `make_architecture.py` now turns CRLF into LF before embedding an SVG logo, and the working copy matches git again; the test passes on Linux from a clean `git archive` of the branch, and the old files reproduce the failure.
- Verification: `make lint` clean; `make docker-test`: 465 passed, nothing skipped, 10 min 39 s; `make readme-check` passes.

### After the merge of Phase 15 (merged as PR #25)

- **The first `v2.0.0` release failed in its Metabase job**: `make docker-demo` could not write `reports/reconciliation.md` (`PermissionError`). The toolkit's container runs as user 1000 and writes to `reports/` and `docs/images/screens/`, bind-mounted from the checkout, which on a GitHub runner belongs to user 1001; Docker Desktop on the development laptop does not check, and no release had run the demo before (the Metabase job is from Phase 11b, after 1.2.0). `make check-env`, which every `docker-*` target runs first, now creates both folders and opens them to the container (`chmod a+w`). Reproduced and checked in Linux containers: a folder owned by 1001 refuses user 1000, and after `check-env` run by its owner, user 1000 writes the report, a subfolder and a screenshot.

## In progress

Phase 15 is merged (PR #24), and the fix for the release (PR #25). `v2.0.0` was tagged again on `main` after the fix. Nothing is in progress.

## Decisions made

### Phase 14b (owner, at planning, 2026-10-14)

1. **Airflow 3.x**: Airflow 2 OSS reached its end of life in April 2026.
2. **Each orchestrator runs its own company in its own namespace** (`af_`, `dg_`), with its own incident store; `make demo` is never touched.
3. **A real 02:00 schedule, paused by default**; the tests trigger runs through the APIs.
4. **Three parts, one PR each** (14b-1 the foundation, 14b-2 Airflow, 14b-3 Dagster and the release gate), so every session ends in a state that can be committed.

### Phase 15 (owner, 2026-10-15)

1. **Not plug-n-play, but adoptable**: no new commands for use outside the simulation; each module's README says what runs as is, what belongs to the simulated company, and what to replace.
2. **The README speaks of version 2 only**: no DuckDB, SQL Server, 1.x results or superseded ADRs. The legacy Postgres and its reports stay, as the input of the migration.
3. **"How the decisions were made" becomes how the problems were seen, framed and solved.**
4. **A professional architecture diagram** in the style of the owner's template, white background, one version, **every logo in full colour from the vendor's own files**.
5. **Screenshots committed** in the repository.

### Phase 15b, made during implementation

- **Old-to-new ADR numbers** (for reading older pull requests and commits): 0002 to 0001, 0003 to 0002, 0004 to 0003, 0006 to 0004, 0008 to 0005, 0009 to 0006, 0010 to 0007, 0011 to 0008, 0012 to 0009, 0013 to 0010, 0014 to 0011, 0015 to 0012, 0016 to 0013, 0017 to 0014, 0018 to 0015, 0019 to 0016, 0020 to 0017; 0001, 0005 and 0007 retired.

- **"Results" keeps its name**: `readme-check` reads sections whose heading starts with "Results", and 15c revives it against the release run.
- **Seven problems, not one per ADR**: each block names the ADRs it rests on; the ADR index stays complete, superseded records included, for history.
- **The link check is a test**, not a one-off script: the SPEC's acceptance asks that every relative link resolves, and a renamed heading would otherwise break a README anchor silently.
- **The reports' ClickHouse database is fixed to `marts`** in `load_specs`; the reconcile guide states it as a limit instead of changing the code in a documentation phase.

### Phase 15a, made during implementation

- **The diagram is generated, not drawn by hand**: reviewed as code, its numbers from the project, a test that fails when it is stale.
- **Logos where the brand kit could not be fetched directly**: Slack's media kit downloads through a portal that needs a browser, and Snowflake's brand page refuses automated requests, so both come from the vendor's own site (Slack's marketing CDN, Snowflake's documentation). dbt's kit and the ASF's Airflow logos ship PNG only. ClickHouse's kit ships black and white only. All recorded in `LICENSES.md`.
- **No Python logo**: the official file carries the wordmark, and the diagram does not need it.
- **Dagster's run page, not its asset graph**: the graph of 120 assets is unreadable at README size.
- **Metabase screenshots are the first 900 px** of each dashboard: Metabase scrolls inside the page, so a full-page capture stops at the viewport.

### Phase 14b-3, made during implementation

- **Dagster 1.13.25 and dagster-dbt 0.29.25**, not 1.13.26: the latest release was six days old; this one two weeks.
- **A pinned, hashed `requirements.txt`** for Dagster's environment: Dagster publishes no constraint file, and the toolkit's `uv.lock` must not carry Dagster.
- **One `dbt build` per run, not one invocation per model**: that is how dagster-dbt runs dbt, and the desk regroups a run either way. Freshness runs first in the same asset function and never holds the build back.
- **The run fails after the ingest when dbt failed** (`raise_on_error=False`, then `Failure`): the hook always gets the artifacts, and the UI shows a faulted run red, as Airflow does.
- **`dagster dev`** in one container, like `airflow standalone`: one machine, not a production deployment; the README says so.
- **The driver named in `dagster.yaml`** (`postgresql+psycopg2`): SQLAlchemy 2.1 defaults `postgresql://` to psycopg 3, which dagster-postgres does not install.
- **No `from __future__ import annotations`** in the definitions: Dagster checks the `context` annotation as a type.
- **Dagster's UI on 127.0.0.1 only**: it has no login in open source. Airflow's has one, so it keeps its port.
- **The release runs both checks on separate runners**, in parallel with the Metabase job, so it waits for the slower one (Airflow, under 9 minutes here).

### Phase 14b-2, made during implementation

- **Freshness on the company's clock** (owner, 2026-10-15, option A): `platform-ops company clock` gives dbt's `simulated_now`; on the wall clock every source of the simulated company would be stale every night.
- **Freshness beside the models, not before them**: a stale feed turns the run red without holding the build back (the plan had it as the first task).
- **The toolkit's dbt is the one Cosmos runs** (`dbt_executable_path`): Airflow's environment holds no dbt, so the two lock files never meet.
- **The manifest is parsed at image build** (`LoadMode.DBT_MANIFEST`): no dbt runs while the DAG is parsed; Cosmos caches the partial parse after the first task, so later tasks skip the full parse (about 10 s each).
- **Tests on two models are detached** (`should_detach_multiple_parents_tests`): on the first healthy run, `relationships` from `stg_payments` to `stg_orders` ran with `stg_orders`'s tests before `stg_payments` existed and failed.
- **No `start_date`** on the schedule (`CronTriggerTimetable`, `run_immediately=timedelta(0)`, `catchup=False`): with one, Airflow 3.3 runs the night that just passed as soon as a fresh stack is unpaused, which made a clean-stack test run twice and attribute the incident to the scheduled run. Without it, a fresh stack waits for the next 02:00, and a stack that was down runs the latest night missed once.
- **The container gets only what it uses**: the Postgres login and the company's `dbt` ClickHouse user, no `.env`; nothing secret is in the DAG.
- **The whole project in the test, no selection**: one run of all 224 tasks takes about 6 minutes at scale 0.01, under the 15 minutes the plan allowed.
- **No screenshot in the README**: the repository tracks no images (screenshots go to `reports/`); the UI was checked through the API instead.

### Phase 14b-1, made during implementation

- **The hooks call the CLI, not the library** (ADR 0017): the toolkit's lock file never meets Airflow's or Dagster's constraints.
- **Run ids name their scheduler** (`airflow:<dag>:<run>`, `dagster:<run>`), so two schedulers feeding one store cannot collide.
- **A missing run id calls nothing**: an ingest under an empty run id would group unrelated invocations.

### Phase 14 (owner, at planning, 2026-10-14)

1. **Split in two**: 14a is the core; 14b is real Airflow (Cosmos) and Dagster (dagster-dbt) in Docker, their integration tests and the release gate.
2. **The incidents happen after cutover, inside the company's own timeline**: three production weeks extend the window to 16, the demo gets longer and every cost figure changes. A separate scenario (a second history of the same company) was the cheaper option, declined.
3. **A demo of about an hour is acceptable.**

### Phase 14a, made during implementation

- **Orphan keys replace duplicate keys**: the raw `ReplacingMergeTree` deduplicates keys by design, so a duplicate cannot reach staging.
- **Faults are planted in the raw copy and fixed by resyncing it from Postgres**, the source of truth; a fault that breaks the loader (a stale feed, a changed table) stops it for that table, and the watermark catches up after the fix.
- **Resolution is observed**: a later run that covered the root and found it healthy (its tests must have run, if it has any); a model that ran before its tests did is not yet healthy.
- **A run is regrouped on every ingest**, and an incident this run opened is merged into another when a later invocation finds the real root above it.
- **Each production invocation clears older result files first**: the first production night once ingested the parallel run's last `dbt build` results as its own.
- **`support_tickets` freshness is off**: at scale 0.01 a quarter of the days have no ticket, and the first test run opened an incident for a quiet day.
- **The suite's faults moved to production days 0 and 1**: on days 1 and 3, the stale feed (an error only after 48 hours) was still open when the week ended.
- **A new failure above an open incident's root appends to it.** The first full run paged twice for one fault: F5 dropped a column, the loader stopped, and two nights later the feed's freshness error (ingested before the night's run) opened a second incident before the run's model error merged it back. A stale feed whose staging model's incident is still open is the same problem, seen from the source.
- **Snapshot tables are merged before every snapshot and build.** The first pair of clean runs left every report identical but differed by 80 bytes read in one snapshot upsert: each night's snapshot appends a part, merged in the background at different moments. The same reason raw is merged every night (Phase 13).
- **The fingerprint leaves out dbt's invocation ids** in the incident tables: they are random by design, like the run id, and keying ingests by them is what makes a re-ingest change nothing.
- **Every missing password is reported at once** (the dashboards' and the ClickHouse users'), not the first one found.

### Phase 13b (owner, at planning, 2026-10-14)

1. **The Incidents page is hidden until Phase 14**, when the incident module moves to the store. The 1.x page file is deleted rather than left broken (it is in git and at `v1.2.0`).
2. **The dashboards read as their own read-only Postgres role**, with its own password and no default, like the company's ClickHouse users.
3. **The Reconciliation page is a timeline** of the 42 nights against the 21 pairs, with one night in detail.
4. **The Cost page has four views**: showback by team and month with a model switch, by workload, the business case, and what to change.

### Phase 13b, made during implementation

- **Everything the cost report states is stored**, so the pages never recompute: the business case and the recommendations were only computed at render time before.
- **Status is a fixed status colour plus a glyph** on the reconciliation grid (✕ failed, ↑ a report whose difference comes from an upstream table), never a team colour and never colour alone.
- **The browser check found three presentation bugs no assertion caught**: two dollar amounts in one caption became a LaTeX formula (dollar signs are now escaped), axis labels were cut off or every other row label dropped (wider label limits, taller grid rows), and rounded cells left dark dots where four corners met (band padding instead).
- **The browser check waits for a page to stay rendered**: Streamlit reports a page done for a moment between `app.py` and the page, and the first cold load of the overview landed on it once.

### Phase 13 (owner, at planning, 2026-10-13)

1. **The raw layer is merged (`OPTIMIZE FINAL`) every night**, after the load, so `FINAL` reads measure the same bytes on every run. Measured at 3.6 s for all nine raw tables at scale 1.0; recorded as the platform team's maintenance work and priced like any other.
2. **Public on-demand list prices with their sources** for the cluster (EC2 `m6i.2xlarge`, EBS gp3) and the legacy reporting replica (RDS `db.m6i.2xlarge` PostgreSQL, RDS gp3), us-east-1, looked up in October 2026. The sizing (one node and 500 GB each) is an assumption, printed first in the report.
3. **Quotas are suggested in the report, never applied.**
4. **Split in two**: 13a is the cost core and `reports/cost.md`; 13b brings the Streamlit dashboards back on the Postgres ops store, with the browser check.

### Phase 13, made during implementation

- **Bytes written are not collected.** `written_bytes` differed between two identical runs (how ClickHouse cuts blocks varies); `written_rows` repeats and is kept.
- **Catalogue statements and reads of system tables are collected but not billed.** dbt's count of `CREATE`, `DROP` and `RENAME` statements differs by one or two between runs, and its reads of `system.*` with them; no warehouse bills either.
- **The run id is a module value, not a `ContextVar`**: the reconciliation compares from a thread pool, and threads do not inherit a context, so its first queries went untagged.
- **Query ids are a stable numbering**, by simulated time, kind of work, subject and the query's own text and measurements: the query log's ids are random.
- **`ALTER USER ... SETTINGS` replaces a user's settings**, so the suggested per-query limit restates the reader profile. Checked by applying the suggestions to a test namespace.
- **Users that also build, load or reconcile get no quota suggestion** (`dbt`, `loader`, `platform`): a limit there could stop the pipeline.
- **Only billable statements are kept and numbered.** dbt sends one or two more or fewer catalogue statements from run to run; numbering them shifted every id after them. They are counted, not stored.
- **dbt's invocation id is taken out of table names.** dbt-clickhouse names an incremental model's staging table `<model>__dbt_new_data_<uuid>`, new every run, and it showed in the tables read and the SQL text. Two runs at scale 0.01 then left every cost ops table and `cost.md` identical.
- **dbt writes one part per partition** (`max_insert_threads = 1`, inserts squashed to one block, in the dbt profile and the `writer` settings profile). The first two clean runs at scale 1.0 differed by a few cents: ClickHouse's default of one insert thread per core wrote the four incremental models (two million rows each) into 20 to 29 parts in a different way on every run, and the same ad hoc query on `marketing_fct_sessions` read 262,491 rows in one run and 246,198 in the other. Scale 0.01 never showed it, because its tables fit in one block.
- **Redaction found a NUL-byte bug**: the comment placeholder held digits, which the number pattern then replaced, and Postgres refused the text. The placeholder now has no digits.

### Phase 12 (owner, at planning, 2026-10-14)

1. **The parallel run is inside the demo company's timeline**, not a separate scenario: one company, one history, and its cost (backfill, nightly builds, the reconciliation's queries) lands in ClickHouse's query log for Phase 13.
2. **6 weeks, cutover after 14 consecutive clean nights.**
3. **Reports reconciled every night**, so dbt builds every night of the parallel run.
4. **Report differences are graded by lineage**: explained by an upstream table that differs the same night, or flagged as a logic difference.
5. **Run time**: measured at 102 s per night one pair at a time and 23 s six at a time; with a 45 to 50 minute estimate, the owner kept 6 weeks at scale 1.0 and asked the release CI to run the demo smaller.

### Phase 12, made during implementation

- **`make demo` runs the parallel run, `make reconcile` writes the sign-off from the recorded nights** (the SPEC's acceptance said `make reconcile` runs the window; changed with the plan, since the window is part of the company's history).
- **Every defect and fault touches data no dbt test checks.** The first full run paged orders, and dbt's relationship tests caught the orphaned order lines and payments; a padded order status failed `accepted_values`. A failing build stays fatal, so the defects moved: the paging bug to web sessions (order lines could not be used either: their keys are `order_id * 10 + line` and never fall on a page boundary), the padding to product names.
- **The float defect's truth was measured against ClickHouse**: `toDecimal64(toFloat64(x), 2)` scales the float by 100 in floating point and truncates. A first model (truncating the float's exact binary value) disagreed with ClickHouse on 245,308 of 575,208 payments and showed as 71% recall; the corrected one agrees on all of them.
- **The golden-row test caught a ClickHouse trap**: `if()` reads a NULL condition as false, so a NULL boolean rendered as `false`.
- **The ops store for the reconciliation is prepared on the first parallel night**, not when the run starts, so a missing password is still reported before anything is touched.
- **Faults are repaired the next day, whether or not the diff found them**: the grading then exposes a miss instead of the simulation hiding it.

### Phase 11b (owner, at planning, 2026-10-13)

1. **The BI replay goes to Phase 13**, with the workload generator: 11b generates the card SQL from the exposures as a shared module, and Phase 13 replays exactly those queries under the `metabase` user.
2. **A separate `loader` user** for the ingestion, so its load is not counted as dbt's.
3. **One password variable per user**, no shared password.
4. **The Metabase check runs on demand and before each release**, not in pull requests.

### Phase 11b, made during implementation

- **Metabase `v0.63.19.6`**: the latest patch of a minor version out for weeks (the 0.64 line was days old). Its ClickHouse driver is bundled; the separate driver repository is archived.
- **No default passwords**: a missing variable stops the run before anything is written. `dbt parse` still works without them, because the dbt invocation falls back to the admin connection only when the dbt password is unset, and parsing never connects.
- **Grants found by running**: `system.parts` for dbt (dbt-clickhouse's `insert_overwrite`), `READ ON POSTGRES` and `CREATE TEMPORARY TABLE` for the loader, `system.processes` for Metabase's driver with a row policy (without it, Metabase could read every user's running query text).
- **Chart hints live in the model's `config.meta`**, next to the SQL its owner reviews; the rules cover everything else.
- **Card names are unique per team collection** ("Dashboard: Model"); on the dashboard each card shows its short title.
- **Metabase's own "Examples" collection and sample database are left alone**; the screenshots skip them.

### Phase 11 (owner, at planning, 2026-10-13)

1. **Split in two.** 11a: legacy Postgres and its reports, ingestion, the dbt project on ClickHouse, the ops store, ownership on the company project. 11b: Metabase from the exposures and one ClickHouse user per team.
2. **One dbt project now.** The company's project moves to ClickHouse in 11a; cost, incidents, the dashboards and reconciliation pause until Phases 12 to 14, and `make demo` says so.
3. **The ops store is Postgres**, database `platform_ops`, on the same server as the legacy database. It is also the Phase 14 store.
4. **One legacy report per dbt exposure**, about 12.

### Phase 11a, made during implementation

- **Money stays in Decimal.** A float discount rate made revenue sums depend on ClickHouse's aggregation order, so two runs differed in the last bits; the rate and resolution hours are now Decimal.
- **Averages are exact and round half away from zero**, like Postgres (`avg_half_up` macro). ClickHouse's `avg` returns a Float64 and `round` on a float rounds half to even, so `avg_order_value` drifted from the legacy reports by cents. The report-versus-mart test found it.
- **Session settings** in the dbt profile and the toolkit's client: `join_use_nulls = 1` (an outer join returns NULL, not a default) and `union_default_mode = DISTINCT`. `prefer_column_name_to_alias` was tried and dropped: it breaks dbt-clickhouse's own metadata queries. Columns an alias would shadow are qualified instead.
- **`allow_nullable_key`** only on the table folders (intermediate, marts, abandoned): the legacy columns are nullable and keys derive from them. Views reject the setting.
- **The query comment goes first** (`append: false`): a seed's `INSERT` carries its CSV rows inline, and a trailing comment was read as one more row.
- **The snapshot uses the timestamp strategy** on `updated_at`: the check strategy stamps rows with the wall clock.
- **Raw tables are not partitioned.** `ReplacingMergeTree` deduplicates only within a partition, and a changed row moves in `updated_at`.
- **Starting over drops every company database** in ClickHouse, because a snapshot survives `--full-refresh`.
- **The reconciliation library still runs** on DuckDB in its tests (self-contained, no warehouse); only its command is paused. SQL Server went from its internals too, since nothing could run it.
- **A `namespace` setting** prefixes every Postgres schema and ClickHouse database, so the tests share the servers with the demo without touching it.

### Version 2.0 (owner, 2026-10-13)

The owner asked why the repository held two dbt projects (the simulated company on DuckDB, and the Phase 10 test fixture on ClickHouse) and redirected the project around one story that mirrors the company's real stack:

1. **One simulated company, one dbt project.** The company's reporting runs on a legacy Postgres database as hand-written SQL views and tables; it migrates to self-managed ClickHouse, where the analytics are rebuilt as one dbt project. The ClickHouse fixture project is retired once the company's project replaces it in the tests.
2. **The toolkit is applied to that flow:** reconciliation signs off Postgres to ClickHouse; ownership and incidents run on the ClickHouse dbt project; cost and FinOps run on ClickHouse's query log.
3. **BigQuery and Snowflake are priced as alternative targets only**, on the same measured workload; no module is built for them, and their collectors stay contract-tested.
4. **Cost uses measured bytes and rows, modelled time.** `system.query_log` gives bytes and rows; compute time stays modelled from bytes, so reports stay byte-identical. If measured bytes do not repeat exactly, ask before relaxing the rule.
5. **DuckDB stops being the warehouse; Iceberg and SQL Server are removed.** Version 1.x stays available at `v1.2.0`.
6. **Order follows the migration:** Phase 11 the company on Postgres and ClickHouse, 12 reconciliation, 13 cost and FinOps, 14 incidents on real runs (the former Phase 11, with the live store, notifiers, integrations and dashboard decided before), 15 documentation and 2.0.0. The former Phases 11 and 12 are folded into 13 and 14.
7. **Every number is regenerated** from 2.0's runs: READMEs, reports, ADR figures, and the numbers in the owner's CV.
8. **Airflow and Dagster for real, in Docker** (owner, same day). The integrations still import neither orchestrator, so the toolkit stays light. But they are also tested on real Airflow with Cosmos and real Dagster with dagster-dbt, in their own images. That runs on demand and in the release workflow before it publishes; pull requests keep the fast tests with fakes. The same images run the simulated company's dbt project on its schedule behind Compose profiles, with their UIs, so the company runs dbt the way the real one does. `make demo` stays on the Makefile. This replaces "tested with fakes only" (decision 5 below), and `docs/CLAUDE.md` was changed to allow these images.
9. **Closer to a real company** (owner asked whether the plan was realistic; all five suggestions taken):
   - ClickHouse-style modelling in Phase 11: `ReplacingMergeTree` for tables changed in place, `MergeTree` with chosen `ORDER BY` and partition keys, incremental models, not nightly full refreshes of 120 models.
   - Incremental loads from Postgres by an `updated_at` watermark through `postgresql()`, not a full copy and not CDC (too heavy for a laptop).
   - A parallel run before cutover in Phase 12: legacy and ClickHouse side by side for weeks, nightly reconciliation, cutover only after N consecutive nights within thresholds.
   - FinOps in Phase 13 splits the cluster's cost into compute (bytes and rows read) and storage (`system.parts`, by rows and uncompressed bytes), compares legacy Postgres's cost before with ClickHouse's after, and shows back per ClickHouse user and team with suggested quotas.
   - One ClickHouse user per team, plus `metabase` and `dbt`, so `query_log` records who ran what.
10. **Metabase is the company's BI, Streamlit stays the toolkit's.** Metabase (Phase 11, behind a Compose profile, its application database on the same Postgres server) shows the dbt exposures as real dashboards on ClickHouse marts, created by a script from the exposures. `make demo` replays their queries deterministically under the `metabase` user; a live Metabase's own queries never feed the reports. Superset was judged too heavy for this.

### Before Phase 11 (owner, 2026-10-13)

1. **The rollout gets a starter registry command** before Phase 11 (`init-registry`, release 1.2.0: a new command is a minor version).
2. **Dashboards for real data are part of Phase 11**: a live mode of the Streamlit app reading the shared Postgres store, starting with incidents. The demo keeps DuckDB.
3. **Slack through a bot token** (`chat.postMessage` to the team's `channel`), Telegram through a bot token to the team's `telegram_chat`.
4. **The company runs dbt with Airflow + Cosmos and Dagster + dagster-dbt.** Cosmos runs one dbt invocation per model, so ingest regroups a whole orchestrator run on every call.
5. **The integrations import neither orchestrator** and are tested with fakes plus a real end-to-end ingest; the SPEC's acceptance was changed to match.

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
2. **The budget-driven designs stay as they are.** The real dbt build every 28 days with daily replay (a 1.x ADR, retired in Phase 15) and the incident scenario that runs dbt only on nights a fault is active (ADR 0005) are documented. Their reports match across the native run, the container and the Phase 6 baseline. Changing them would redo every reference number and README figure for realism alone. The pandas and pyarrow caps were already lifted in Phase 7.

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

1. **Logical bytes, not stored bytes** (a 1.x ADR, retired in Phase 15). Compressed size is not stable between identical runs.
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

For version 2.0.0 (design limits; the ADRs and module READMEs say where):

- **The modules are an engine, not a product, outside the simulation.** `cost report`, `reconcile run`, `incidents run` and the Streamlit pages read the simulated company's tables; each module's "Adopting it" section says what runs on a real platform as it is (the ownership check, `incidents ingest` and the hooks) and what has to be wired.
- **`Settings` requires every section**, the simulation's included, for every command but `metadata check --manifest`; the image's bundled config works for `incidents ingest`.
- **The reports' ClickHouse database is named `marts`** in `reconcile.specs.load_specs`.
- **At scale 1.0 an incremental build is not faster than a full refresh** (about 290 nodes of fixed overhead dominate), and the two incremental intermediate models read more with every build because raw is sorted by key, not date.
- **The nightly merge of raw is about a fifth of the cluster's compute** ($228.57 of $1,047.73). It is what makes reads repeat; a real team would merge less often or deduplicate with `argMax` in staging.
- **Grouping is per run.** Two unrelated faults failing the same downstream model in one run attach it to one of them by tie-break (ADR 0005).
- **No incident closes in less than 24 hours**, because resolution waits for the next nightly run; a rerun after a fix would close it sooner.
- Filters written against a CTE or subquery column outside it are not traced to the base table for hotspot detection. The workload and dbt do not write filters that way.
- Every Metabase line ends with a partial period, because the window ends at 02:00 on its last day; daily rollups chart only their last 91 days, under Metabase's 2,000-row limit.
- **The orchestrator stacks are for one machine**: `airflow standalone` and `dagster dev`; Dagster's UI has no login and is published on 127.0.0.1 only. The Airflow image is 5.51 GB unpacked.
- **The screenshots in `docs/images/screens/` are from the 2.0.0 release run** and must be retaken (`make docker-docs-images`) when a page changes; the architecture diagram is checked by a test, the screenshots are not.
- `make docker-browser-check`, `readme-check` and the Metabase check stay out of pull request CI: they need a full run. The release runs Metabase and both orchestrators; `readme-check` needs a scale 1.0 run, so it runs by hand before a release.

## Backlog

Improvements the owner asked to note after 2.0.0, not planned yet.

### Dashboard cost by viewer (owner, 2026-10-19)

The cost module charges a dashboard today, but not the way its viewers use it:

1. **The simulation only has scheduled refreshes, no one opening a dashboard.** Each dashboard is refreshed every 12 or 24 hours after the 02:00 run (`simulation/workload.py`), so a dashboard's cost today is the cost of keeping it fresh, not the cost of the views it gets.
2. **The owner pays, not the viewer.** A refresh is consumption charged to the team that owns the dashboard (`cost/attribution.py`): a finance dashboard that sales opens all day is paid by finance alone.
3. **In real life it is harder.** Every query the BI tool sends runs as its one service user, so the warehouse's query log sees the BI tool, not the person viewing; and a view served from the BI tool's cache sends no query at all, so there is nothing to charge.

Directions discussed, none decided:

- **A. Keep the owner paying** (today): simple, and the owner has a reason to tune the dashboard.
- **B. Share each dashboard's measured cost across teams by views**, from the BI tool's own view log (who opened which dashboard, when), mapped from BI users to teams; scheduled refreshes shared the same way, since they exist for the viewers. Totals still add up to the bill. The assistant's recommendation.
- **C. Charge each query to the person who triggered it**, where the BI tool writes the user's id into the query it sends; exact, and cache hits cost nothing, but it depends on the driver keeping that comment (to check first), and scheduled refreshes still need A or B.

Any of them needs the simulation to model people opening dashboards, by team, so the demo shows the difference.

### Incidents (owner, 2026-10-19)

How the desk works today: it reads each dbt invocation's results as soon as that step of the pipeline finishes (after each of a production night's four invocations, after each Cosmos task, after Dagster's build), regroups the whole run, and pages at once; no second scan. The gaps:

1. **An incident resolves only at the next scheduled run.** Owners fix within hours, but the incident waits for the next night's run to show its root healthy, so none closes in less than 24 hours. There is no way to rerun the root's checks right after a fix.
2. **No acknowledgement or escalation in real use.** A page goes to one person, the owner: no on-call rotation, no backup, no escalation when nobody acknowledges, and no acknowledgement recorded from the chat. The time to acknowledge and to fix is simulated (`incidents/lifecycle.py`).
3. **Only what a test or a freshness check catches.** Wrong data no test checks is invisible, and a stopped feed is found only once the freshness rule's two days are over (64 hours in the release run).
4. **Grouping stays within one run.** Two unrelated faults failing the same downstream node in one run attach it to one of the two roots, by tie-break.
5. **The routing exception is narrow.** Only a staging model reading exactly one source pages the source's owner; when broken source data first shows in an intermediate model or a mart, the page goes to that model's owner.
6. **Severity comes from lineage alone.** The score weighs what sits downstream by tier, not the moment (a finance month-end close) or how long the data has been broken.
7. **The load is shown, not balanced.** 6 of the 8 pages went to the one person who owns most feeds; the desk measures that, but has no secondary owner or way to share the load.

## Open questions for the owner

None open.

## Next step

1. Check that the second `v2.0.0` release published: the image `2.0.0` on GHCR and the GitHub Release with the changelog's notes.
2. If a release job failed again, fix it on a branch, then delete and push the tag again (a failed release publishes nothing); the `orchestrators` workflow can be run by hand to look at an orchestrator job.
3. Pilot rollout of the ownership check on a company dbt repository, with the 2.0.0 image: follow "Adopting it" in the metadata README and record the baseline here (numbers only).
4. Decide on dashboard cost by viewer (Backlog): A, B or C, then a phase to build it.
5. Decide which incident gaps to close (Backlog), then a phase to build them.
