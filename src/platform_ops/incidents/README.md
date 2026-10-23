# incidents

When one bad raw table fails a dozen checks, who gets woken up, and how many times? The company's last three weeks are production on ClickHouse, after the migration's parallel run. Every night runs dbt's source freshness, the sources' tests, run, snapshot and the other tests, and things go wrong the way they do in production: eight faults in operation, from a loader writing NULLs to a feed that stops. The incident desk ingests every invocation as it finishes and turns the failing checks into incidents: one per real problem, scored by how much of the business it reaches, paged to the person who can fix it, and resolved on its own once a later run finds the root healthy: the rerun the owner's fix starts ([ADR 0016](../../../docs/adr/0016-incidents-on-the-companys-production-runs.md)).

```bash
make demo                 # the company's 16 weeks, the production weeks included
make incidents            # reports/incidents.md and a postmortem per SEV1, from the store
uv run platform-ops incidents summary     # failing checks against pages, per person and week
uv run platform-ops incidents ingest --target <dbt target> --run-id <run>   # any dbt run
```

## How it fits together

| Step | Module | Output |
|---|---|---|
| Break | `simulation/faults.py`: eight faults in operation on ClickHouse's raw layer, with their ground truth; the fix resyncs the table from Postgres | `ops.incident_faults` |
| Run | `simulation/company.py`: every production night's freshness, source tests, run, snapshot and tests, and the rerun after each fix, each handed to the desk | dbt's artifacts |
| Ingest | `ingest.py`: model errors, test failures and freshness errors from any adapter's `run_results.json` and `sources.json` | `incident_invocations`, `incident_results`, `incident_check_events` |
| Group | `grouping.py` (pure) and `desk.py`: one incident per root, the whole run regrouped on every ingest | `incidents`, `incident_run_links` |
| Score | `severity.py`: root tier plus tier-weighted downstream reach, SEV1 to SEV3, thresholds from an optional `incidents.yaml` | `incidents` |
| Route | `routing.py`: the root's owner, or the source owner when the root is a staging model | `incidents` |
| Resolve | `desk.py`: a later run that covered the root and found it healthy | `incidents` |
| Notify | `notify.py`: Slack and Telegram bots per team, after commit, delivery recorded | `incident_notifications` |
| Respond | `production.py`, `lifecycle.py`: the company's simulated owners acknowledge and fix on a modelled schedule | `incident_responses` |
| Grade | `metrics.py`: fault to incident mapping, routing accuracy, MTTD, MTTR, pages per person | `ops.incident_metrics`, `ops.fault_incidents` |
| Report | `report.py`, `summary.py` | `reports/incidents.md`, `reports/postmortems/INC-*.md` |

The incident tables live in the Postgres ops store, versioned (`store.py`), and every ingest holds one lock, so concurrent ingests never interleave. Nothing but `metrics.py` reads the ground truth: the desk finds its incidents from what dbt reports, as a real team would.

## The decisions that shape the numbers

**`dbt run` then `dbt test`, not `dbt build`.** `dbt build` skips everything below a failed test, so the downstream checks that make up an alert storm would never run. Separate invocations let bad data flow downstream as it does in production, and the grouping has something real to group ([ADR 0005](../../../docs/adr/0005-incident-grouping-severity-and-routing.md)).

**One incident per root, across invocations.** A failed node whose ancestors are all healthy is a root, and every other failure belongs to the root above it. A run is every invocation that shares its run id, regrouped on every ingest, so a run split one invocation per model (as Cosmos runs it) gives the same incidents as one build, whatever order the pieces arrive in. When a later invocation shows an earlier root was not the real one, its incident is merged into the real root's.

**Page whoever can fix it.** Staging models only rename and cast, so a staging failure almost always means the data arrived broken. The page goes to the source owner, not to whoever wrote the rename.

**Resolved by a run, not by a promise.** An incident resolves when a later run covers its root and finds it healthy: the model ran or the source is fresh again, and its tests passed. The owner's fix is not enough on its own, so the fix starts the run that confirms it: the root and everything below it are rebuilt as soon as the fix lands (`rerun-<incident>`), and the dashboards are right again then, not the next morning. A failure that rerun finds below a root it did not check (another incident's source, still broken) joins that incident instead of paging again.

**A stopped feed is caught the first night.** Freshness lets a feed be quiet for two days before it errors, so a stopped feed was found after 64 hours. Every continuous feed now has a volume test (`dbt/tests/generic/recent_volume.sql`): the rows loaded in the last 24 hours must be at least half the median of the seven days before, on the data's own clock, the raw layer's latest load. Sources are checked before the models are built, so a broken feed is known before the models it breaks fail and page someone else. Wrong values in a column no test checks are still invisible.

**Faults in operation, fixed from the source.** The faults are planted in ClickHouse's raw layer, the copy, never in Postgres, the source of truth, so the fix a team applies is to resync the copy. `ReplacingMergeTree` deduplicates raw by key, so duplicate keys cannot happen there; orphan payments take their place.

## Results at scale 1.0

From two clean runs of `make docker-demo`, which wrote byte-identical `incidents.md` files and postmortems. The production weeks are 21 nights; 8 had failing checks, and each of the 8 fixes started a rerun.

| Measure | Count |
|---|---:|
| Faults in operation | 8 |
| Failing checks (raw alerts) | 39 |
| Incidents, one per fault | 8 |
| Pages sent | 8 |
| Updates on incidents still open | 6 |

| Routing rule | Right person | Right team |
|---|---|---|
| Staging roots page the source owner | 8 of 8 | 8 of 8 |
| Page the root node's owner | 4 of 8 | 8 of 8 |

| Severity | Incidents | Mean MTTD (h) | Mean MTTR (h) |
|---|---:|---:|---:|
| SEV1 | 2 | 16.0 | 7.1 |
| SEV2 | 5 | 16.0 | 20.9 |
| SEV3 | 1 | 16.0 | 60.1 |

What the numbers say:

- **39 failing checks became 8 pages, one per problem.** Without grouping, marco would have been paged 27 times in three weeks, mostly by tests on the models below two broken payment feeds; with it, twice.
- **Paging whoever can fix it moves the load, and shows where it piles up.** The staging rule pages the right person 8 times out of 8, the root's owner only 4. But 6 of the 8 pages land on priya, who owns most of the feeds. Grouping does not spread that load; it makes it visible, which is the case for sharing source ownership before the pages do.
- **The rerun took the schedule out of MTTR.** Each incident resolves the moment its fix lands, because the fix starts the run that confirms it: the first SEV1 in 6.9 hours, where waiting for the next night took a full day. The mean is 22.3 hours; what is left is the owners' own time, and the slowest, the SEV3, waited 60.1 hours for its fix. Faster people, or a backup owner, is now the lever.
- **A stopped feed is caught the first night.** Every fault was detected by the first nightly run after it happened, 16 hours, the stopped web sessions feed included: the volume test failed on its first quiet night, where the freshness rule alone took close to three days. The marketing spend feed, stopped by a dropped column, is now rooted at its source, which pages the same owner a night's checks earlier.

## Adopting it

`incidents ingest` is the same code the company's desk runs, and it reads nothing but what dbt writes, so it works on any dbt project, adapter and scheduler.

- **Runs as is:**
  - `platform-ops incidents ingest --target <dbt target folder> --run-id <run id> [--at <time>] [--registry-dir <folder>]`, after every dbt invocation. It reads `run_results.json`, `sources.json` and `manifest.json` from any adapter, stores everything in the Postgres store (`store.py` creates and migrates its tables), regroups the run, and sends what it opened, appended or resolved.
  - The hooks for Airflow with Cosmos and Dagster with dagster-dbt (`platform_ops.integrations`, standard library only), with the run id contract in the [orchestrators README](../../../orchestrators/README.md).
  - Slack and Telegram, switched on by `SLACK_BOT_TOKEN` and `TELEGRAM_BOT_TOKEN`, addressed by each team's `channel` and `telegram_chat` in `teams.yaml`.
  - Your own severity thresholds and tier weights in an `incidents.yaml` next to the registry (`IncidentPolicy`).
- **Belongs to the simulated company:**
  - `incidents run`: its report and postmortems are graded against the faults the simulation planted (`ops.incident_faults`) and read the company's run.
  - `incidents summary` reads the bundled project's manifest.
  - `production.py` plays the company's owners (acknowledging, fixing) and `simulation/faults.py` breaks raw on purpose.
  - The Incidents page of the dashboards reads the graded tables.
- **What you replace:**
  - **The registry:** your `teams.yaml` and `ownership.yaml`, passed with `--registry-dir`, because routing is the registry: without owners every incident goes to the platform team.
  - **The Postgres** the store lives in (`POSTGRES_*`), and a namespace (`PLATFORM_OPS_NAMESPACE`) if it shares a server.
  - **The settings file:** the toolkit's image carries one that works as it is; the sections about the simulated company are read but not used by `ingest`.
  - **The report:** yours, written over the store's tables (`incidents`, `incident_check_events`, `incident_notifications`) instead of `incidents run`.
- **In order:**
  1. Get the ownership check passing on your project ([metadata](../metadata/README.md#adopting-it)).
  2. Ingest one night's target folder by hand and read the `incidents` table.
  3. Wire the hook or the call after each invocation.
  4. Set a chat token last, once the incidents look right.
- **Traps we hit:**
  - **Every invocation of one run must share the run id**, or a run split per model looks like many runs, each opening its own incidents.
  - **Give each invocation its own target folder,** or clear old result files first: a `run_results.json` left from an earlier build is read as part of tonight (Cosmos and dagster-dbt already use one folder per invocation).
  - **An incident resolves only when a later run ran its root's tests** and found them healthy: a run that never touched the model cannot close it. Rerun the root and everything below it right after a fix and ingest that run under a run id of its own; otherwise the time to resolve is bounded by the schedule.
  - **A rerun covers part of the graph.** A model it rebuilds may also read another incident's source, still broken; the desk files that failure under the open incident whose root the rerun did not check, instead of paging again.
  - **Freshness alone finds a stopped feed late.** Add the volume test to every continuous feed (`recent_volume`, copied from `dbt/tests/generic`), and run the sources' tests before the models, so the source is the root before anything below it fails.
  - **dbt-clickhouse sets `join_use_nulls`:** in a left join, a day with no rows is NULL, not 0, and a median skips it. The volume test counts empty days as 0, or the day after a big load looks like a drop.
  - **A freshness failure above an open incident is absorbed into it,** not paged again; without that, a stale feed and the failures it causes page twice.

The tests ingest artifacts recorded from a Snowflake build and a BigQuery night of a small shop project (`tests/fixtures/dbt_artifacts`, written by `scripts/make_artifact_fixtures.py`), Cosmos-style partial invocations in both orders, and two ingests at once; both orchestrator stacks run a fault end to end before every release.
