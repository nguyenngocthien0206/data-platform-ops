# incidents

When one bad raw table fails a dozen checks, who gets woken up, and how many times? The company's last three weeks are production on ClickHouse, after the migration's parallel run. Every night runs dbt's source freshness, run, snapshot and test, and things go wrong the way they do in production: eight faults in operation, from a loader writing NULLs to a feed that stops. The incident desk ingests every invocation as it finishes and turns the failing checks into incidents: one per real problem, scored by how much of the business it reaches, paged to the person who can fix it, and resolved on its own once a later run finds the root healthy ([ADR 0019](../../../docs/adr/0019-incidents-on-the-companys-production-runs.md)).

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
| Run | `simulation/company.py`: every production night's freshness, run, snapshot and test, each handed to the desk | dbt's artifacts |
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

**`dbt run` then `dbt test`, not `dbt build`.** `dbt build` skips everything below a failed test, so the downstream checks that make up an alert storm would never run. Separate invocations let bad data flow downstream as it does in production, and the grouping has something real to group ([ADR 0008](../../../docs/adr/0008-incident-grouping-severity-and-routing.md)).

**One incident per root, across invocations.** A failed node whose ancestors are all healthy is a root, and every other failure belongs to the root above it. A run is every invocation that shares its run id, regrouped on every ingest, so a run split one invocation per model (as Cosmos runs it) gives the same incidents as one build, whatever order the pieces arrive in. When a later invocation shows an earlier root was not the real one, its incident is merged into the real root's.

**Page whoever can fix it.** Staging models only rename and cast, so a staging failure almost always means the data arrived broken. The page goes to the source owner, not to whoever wrote the rename.

**Resolved by a run, not by a promise.** An incident resolves when a later run covers its root and finds it healthy: the model ran and its tests passed, or the source is fresh again. The owner's fix is not enough on its own, so MTTR includes the wait for the next run, which is what the business actually experiences.

**Faults in operation, fixed from the source.** The faults are planted in ClickHouse's raw layer, the copy, never in Postgres, the source of truth, so the fix a team applies is to resync the copy. `ReplacingMergeTree` deduplicates raw by key, so duplicate keys cannot happen there; orphan payments take their place.

## Results at scale 1.0

From two clean runs of `make docker-demo`, which wrote byte-identical `incidents.md` files and postmortems. The production weeks are 21 nights; 9 had failing checks.

| Measure | Count |
|---|---:|
| Faults in operation | 8 |
| Failing checks (raw alerts) | 34 |
| Incidents, one per fault | 8 |
| Pages sent | 8 |
| Updates on incidents still open | 4 |

| Routing rule | Right person | Right team |
|---|---|---|
| Staging roots page the source owner | 8 of 8 | 8 of 8 |
| Page the root node's owner | 3 of 8 | 8 of 8 |

| Severity | Incidents | Mean MTTD (h) | Mean MTTR (h) |
|---|---:|---:|---:|
| SEV1 | 2 | 16.0 | 24.0 |
| SEV2 | 5 | 25.6 | 33.6 |
| SEV3 | 1 | 16.0 | 72.0 |

What the numbers say:

- **34 failing checks became 8 pages, one per problem.** Without grouping, marco would have been paged 28 times in three weeks, mostly by tests on the models below two broken payment feeds; with it, twice.
- **Paging whoever can fix it moves the load, and shows where it piles up.** The staging rule pages the right person 8 times out of 8, the root's owner only 3. But 6 of the 8 pages land on priya, who owns most of the feeds. Grouping does not spread that load; it makes it visible, which is the case for sharing source ownership before the pages do.
- **The schedule, not the people, sets the floor of MTTR.** The simulated owners land most fixes within hours (the first SEV1 was fixed under 7 hours after detection), but nothing resolves until the next nightly run finds the root healthy, so no incident resolved in less than 24 hours. A rerun on demand after a fix is the cheapest way to cut MTTR here, cheaper than faster people.
- **A stale feed is caught late by design.** Every fault was detected by the first night after it happened (16 hours), except the stopped web sessions feed: the freshness rule allows 48 hours, so it took 64.0. Tightening the rule would catch it a night earlier, and page someone on every quiet night.

## On a real dbt project

`incidents ingest` is the same code the company's desk runs. Point it at a dbt target folder after each invocation, with the orchestrator's run id, and it does the rest; `--registry-dir` takes the consuming repository's registry, and an `incidents.yaml` there sets its own severity thresholds. Set `SLACK_BOT_TOKEN` and `TELEGRAM_BOT_TOKEN` to page the teams in `teams.yaml`. The tests ingest artifacts recorded from a Snowflake build and a BigQuery night of a small shop project (`tests/fixtures/dbt_artifacts`, written by `scripts/make_artifact_fixtures.py`), Cosmos-style partial invocations in both orders, and two ingests at once.

With an orchestrator, one hook does it (`platform_ops.integrations`, standard library only, so it imports into any scheduler's environment without the toolkit's dependencies):

- **Airflow with Cosmos**: `cosmos.ingest_after_task` as the operators' `callback`. Cosmos calls it after every task, failed or not, with that task's `target/`; the run id is `airflow:<dag id>:<run id>`, so a DAG run of a few hundred tasks is regrouped as one run.
- **Dagster with dagster-dbt**: `dagster.ingest_dbt_invocation(invocation, context)` after each `dbt.cli(...)` in a `@dbt_assets` function; the run id is `dagster:<run id>`.

Both stacks run the company's project for real in Docker, each on a company of its own, and a fault through either opens one incident, pages it and resolves it after the fix; the release waits for both ([orchestrators](../../../orchestrators/README.md)).
