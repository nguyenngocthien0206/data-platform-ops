# incidents

When one bad raw table fails a dozen checks, who gets woken up, and how many times? `make incidents` breaks the simulated company's raw data on purpose, eight times over three simulated weeks. It runs dbt the way production would, and turns the failures into incidents: one per real problem, scored by how much of the business it reaches, and paged to the person who can fix it. Then it grades itself against the faults it injected and writes `reports/incidents.md`, plus a postmortem draft for every SEV1.

## How it fits together

| Step | Module | Output |
|---|---|---|
| Break | `simulation/faults.py`: eight labelled faults covering all six SPEC types, and a repair for each | `ops.fault_ground_truth` |
| Run | `scenario.py`: fresh seed, green baseline, then 21 simulated nights of loads, faults and dbt runs | `ops.incident_runs` |
| Ingest | `ingest.py`: model errors, test failures and freshness errors from dbt's result files | `ops.check_events` |
| Group | `grouping.py`: one incident per root cause, appended to while it is still open | `ops.incidents`, `ops.incident_impact` |
| Score | `severity.py`: root tier plus tier-weighted downstream reach, SEV1 to SEV3 | `ops.incidents` |
| Route | `routing.py`: the root's owner, or the source owner when the root is a staging model | `ops.incidents` |
| Notify | `notify.py`: local by default, Slack if `SLACK_WEBHOOK_URL` is set | `ops.notifications` |
| Respond | `lifecycle.py`: simulated acknowledgement and fix times | `ops.incidents` |
| Grade | `metrics.py`: fault to incident mapping, routing accuracy, MTTD, MTTR, pages per person | `ops.incident_metrics`, `ops.fault_incidents` |
| Report | `report.py` | `reports/incidents.md`, `reports/postmortems/INC-*.md` |

Nothing but `metrics.py` reads the ground truth. The scenario finds its incidents the way a real team would, from dbt's output alone.

## The decisions that shape the numbers

**`dbt run` then `dbt test`, not `dbt build`.** `dbt build` skips everything below a failed test, so the downstream checks that make up an alert storm would never run. Separate run and test let bad data flow downstream as it does in production, and the grouping has something real to group (ADR 0008).

**One incident per root.** A failed node whose ancestors are all healthy is a root. Every other failure belongs to the root above it, and a diamond always resolves the same way. A test that reads two models, such as a relationships test, sits below both, so an orders problem does not open a second incident on order items.

**Page whoever can fix it.** Staging models only rename and cast, so a staging failure almost always means the data arrived broken. The page goes to the source owner, not to whoever wrote the rename.

**Run only what the faults touch.** dbt runs on the nights a fault is active, and only on `@source:raw.<table>`: everything downstream of the faulted source plus the parents of those models, so nothing is compared against a stale table. The end-to-end test checks that on every fault night a full run finds exactly the same failing checks.

## Results at scale 1.0

From two actual runs of `platform-ops incidents run` at scale 1.0, which wrote byte-identical `incidents.md` files and postmortems. Across three timed runs the command took between 2 minutes 22 seconds and 2 minutes 56 seconds. dbt ran on 8 of the 21 nights, 19 invocations in all.

| Measure | Count |
|---|---:|
| Faults injected | 8 |
| Failing checks (raw alerts) | 18 |
| Incidents, one per fault | 8 |
| Pages sent | 8 |
| Updates on incidents still open | 4 |

| Routing rule | Right person | Right team |
|---|---|---|
| Staging roots page the source owner | 8 of 8 | 8 of 8 |
| Page the root node's owner | 3 of 8 | 8 of 8 |

| Severity | Incidents | Mean MTTD (h) | Mean MTTR (h) |
|---|---:|---:|---:|
| SEV1 | 2 | 16.0 | 5.9 |
| SEV2 | 5 | 20.8 | 20.9 |
| SEV3 | 1 | 16.0 | 60.1 |

| Person | Pages before grouping | Pages after |
|---|---:|---:|
| marco | 13 | 2 |
| priya | 1 | 6 |
| kenji | 2 | 0 |
| alice | 1 | 0 |
| sam | 1 | 0 |

What the numbers say:

- **Grouping cut 18 alerts to 8 pages.** The storm is smaller than the word suggests, because most downstream tests check keys and row counts that a few bad values do not break. The volume drop in `raw.orders` is the clearest case: five failing checks across staging, sales and finance models, one incident.
- **Routing matters more than grouping here.** Before grouping, 13 of 18 alerts went to marco, who owns the staging models. Only the 3 about payments were about a source marco owns. The naive rule (page the root's owner) gets the team right every time and the person right only 3 times in 8. Paging the source owner gets all 8 right and moves the load to priya, who owns six of the eight faulted sources.
- **Ownership concentration shows up as time.** Every incident pages someone on the platform team, as ADR 0002 predicted, and an owner who already has an incident open is modelled as slower on the next one. The support tickets incident was opened in the same run as the SEV1 on orders, for the same owner, and was the slowest SEV2 to resolve at 36.9 hours; the other four took 11.2 to 24.6. The random spread in response times plays a part too, so one run shows the direction, not the size, of the effect.
- **Detection is bounded by the schedule.** Faults land at 10:00 and dbt runs at 02:00, so the fastest possible MTTD is 16 hours, and seven of eight faults hit it. The stale source took 40, because freshness only errors after 48 hours without data.

## Running it

```bash
make incidents                      # the scenario, tables, report and postmortems
```

Slack is off unless `SLACK_WEBHOOK_URL` is set in the environment or in `.env`. With it set, every page, update and resolution is also posted to that webhook; a failed post is logged and never stops the run.

Tunables live under `incidents:` in `config/settings.yaml`: the scenario length, the hour faults land, the severity thresholds and the response-time model. The severity thresholds were calibrated on this project's lineage (ADR 0008); a different graph needs new ones.
