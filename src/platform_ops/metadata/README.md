# metadata

The shared layer every other module reasons about: who owns each dataset, and how data flows between them. Cost attribution uses it to send a bill to the right team and to avoid calling a table unused when it feeds one that is used. Incident management uses it to find the root cause of a cascade of failures and to route the incident to the person who can fix it.

## Ownership registry (`registry.py`, `check.py`)

`config/teams.yaml` lists five teams and their members: the four business teams (sales, marketing, finance, product) and a data platform team. `config/ownership.yaml` maps glob patterns on dbt unique ids to an owner, a team and a tier (`critical`, `important` or `best_effort`). It is CODEOWNERS for data.

When several rules match, the most specific one wins: the most literal characters, then the fewest wildcards. Two rules that tie are an error, never a coin toss decided by line order, because a reorder in a pull request should not be able to hand a critical dataset to a different on-call rotation. The full rule and the reasoning behind the platform team are in [ADR 0002](../../../docs/adr/0002-ownership-resolution-and-platform-team.md).

`platform-ops metadata check` fails when any source, seed, snapshot, model or exposure has no owner, when ownership is ambiguous, when a rule names an unknown team or an owner who is not on that team, or when a dashboard's owner in dbt disagrees with the registry. It warns about rules that match nothing. Every finding says which file to change and how, for example a ready-to-paste rule for an unowned model. On success it writes the resolved owner of every dataset to `ops.node_ownership`, so later modules join on ownership in SQL. With `--parse` it builds the dbt manifest first, which needs no data, so the check runs in CI.

A team in `teams.yaml` names its Slack `channel` and, optionally, a `telegram_chat`, for teams that take their alerts on Telegram.

## On a company dbt repository

The same check guards any dbt project, on any adapter, without touching a warehouse ([ADR 0014](../../../docs/adr/0014-the-ownership-gate-in-the-consuming-repository.md)). The repository keeps its own registry next to the project, conventionally in an `ownership/` folder with `teams.yaml` and `ownership.yaml`; names, channels and chats of the company never enter this toolkit's repository. Its CI runs `dbt parse`, which needs the adapter but no connection, and hands the manifest to the toolkit's image:

```bash
platform-ops metadata check \
  --manifest target/manifest.json --registry-dir ownership \
  --format github --summary-json ownership-summary.json [--report-only]
```

With `--manifest` the check reads no settings, opens no database and writes nothing but the optional summary. `--format github` turns each finding into an annotation on the pull request, on the model's file or on the rule's line in `ownership.yaml`. The exit code is 0 when every dataset has one valid owner, 1 when there are owners to fix, and 2 when the check cannot run (no manifest, an invalid registry), so a broken setup is never mistaken for a missing owner.

Roll it out in two steps, because a check that blocks every merge on day one gets switched off. First run it with `--report-only`: problems show on pull requests, nothing is blocked, and `--summary-json` records the baseline, the share of datasets with an owner per resource type. When the registry is complete, drop `--report-only` and make the job required in branch protection. [`examples/github-actions/ownership-check.yml`](../../../examples/github-actions/ownership-check.yml) is a workflow to copy, written for a ClickHouse project; another adapter only changes the `pip install` line and the parse-only profile.

`tests/fixtures/dbt_clickhouse` is a small project on ClickHouse, laid out nothing like the bundled one, with its registry in its own `ownership/` folder. The tests parse it and run the check against variants of its registry, and run it for real on the ClickHouse service (`docker compose --profile clickhouse up -d`).

## Lineage (`lineage.py`, `manifest.py`)

`manifest.py` reads dbt's `manifest.json` into small typed nodes. `lineage.py` builds a NetworkX graph from them, with an edge from every parent to every child across sources, models, tests and exposures, and provides:

- `upstream` and `downstream`: every ancestor or descendant of a node.
- `nearest_failed_ancestor`: the closest failed node above a given one. Ties, which happen in diamond-shaped dependencies, go to the smallest unique id, so the same failures always attach to the same root.
- `downstream_consumers`: the models and dashboards a node feeds, each weighted by its tier using `metadata.tier_weights` in `config/settings.yaml`.

`platform-ops build` refreshes `ops.lineage_edges` after every dbt build, replacing the table in one transaction so it never mixes edges from an old manifest with a new one.

## Results from the first full build

From an actual `make build` followed by `platform-ops metadata check` at scale 1.0:

| | |
|---|---:|
| Sources owned | 9 of 9 |
| Models owned | 118 of 118 |
| Exposures owned | 12 of 12 |
| Rows in `ops.node_ownership` | 139 |
| Edges in `ops.lineage_edges` | 372 |

| Team | Datasets owned |
|---|---:|
| platform | 34 |
| product | 27 |
| sales | 26 |
| marketing | 26 |
| finance | 26 |

11 datasets are critical, 127 important and 1 best effort. The best-effort one is `finance_fct_revenue_v0`, an abandoned model whose name still matches the critical revenue rule and which an exact rule demotes. The platform team owns more datasets than any business team, and all of them sit upstream of everything else, which is the load Phase 3 is expected to measure.
