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

[`examples/github-actions/ownership-check.yml`](../../../examples/github-actions/ownership-check.yml) is a workflow to copy, written for a ClickHouse project; another adapter only changes the `pip install` line and the parse-only profile.

### Rolling out on a repository

A check that blocks every merge on day one gets switched off by the end of the week, and a registry written by hand for hundreds of models never gets finished. The rollout is ordered to avoid both.

1. **Write a starter registry.** In the dbt repository, after `dbt parse`:

   ```bash
   platform-ops metadata init-registry --manifest target/manifest.json --out ownership/
   ```

   It writes one rule per group, not per dataset: each source system, each folder of models (split by name prefix where a folder mixes `dim_` and `fct_`), each installed package, the seeds, the snapshots, and each exposure with the owner its dbt definition names. Every rule says `owner: TODO, team: TODO`. On the bundled project, 141 datasets come out as 65 rules. It never overwrites an existing registry without `--force`.
2. **Name the teams and owners, sources and staging first.** Fill in `ownership/teams.yaml`, then the rules for sources and staging: they hold most root causes, so they decide who gets paged once incidents run on this registry (Phase 11). Split a rule when a group has several owners; the more specific rule wins.
3. **Add the workflow in report-only mode** (`REPORT_ONLY: "true"`). Problems show on pull requests and nothing is blocked.
4. **Record the baseline** from the `ownership-summary` artifact: `owned_share` is the share of datasets whose rule names a real team and member. A rule still on `TODO` counts as unowned, so the starter registry reads 0%, and the number rises as the team works through the list.
5. **Close the TODOs.** `metadata check --report-only` lists one problem per remaining rule, each pointing at its line in `ownership.yaml`.
6. **Make it required.** Set `REPORT_ONLY: "false"` and add the job to the branch protection rules. From then on, a new model cannot merge without an owner, which is the whole point.

`tests/test_ownership_standalone.py` runs the check this way on the company's own project, which is on ClickHouse since version 2.0: it parses the project, then checks variants of its registry through the real CLI, with `--manifest`, `--registry-dir` and `--format github`. Until 2.0 a small fixture project on ClickHouse played that part.

## Lineage (`lineage.py`, `manifest.py`)

`manifest.py` reads dbt's `manifest.json` into small typed nodes. `lineage.py` builds a NetworkX graph from them, with an edge from every parent to every child across sources, models, tests and exposures, and provides:

- `upstream` and `downstream`: every ancestor or descendant of a node.
- `nearest_failed_ancestor`: the closest failed node above a given one. Ties, which happen in diamond-shaped dependencies, go to the smallest unique id, so the same failures always attach to the same root.
- `downstream_consumers`: the models and dashboards a node feeds, each weighted by its tier using `metadata.tier_weights` in `config/settings.yaml`.

`platform-ops build` refreshes `ops.lineage_edges` after every dbt build, replacing the table in one transaction so it never mixes edges from an old manifest with a new one.

## Results from the first full build

From an actual `make demo` at scale 1.0 (version 2.0: the company's project on ClickHouse, results in the Postgres ops store):

| | |
|---|---:|
| Sources owned | 9 of 9 |
| Models owned | 118 of 118 |
| Exposures owned | 12 of 12 |
| Seeds owned | 1 of 1 |
| Snapshots owned | 1 of 1 |
| Rows in `ops.node_ownership` | 141 |
| Edges in `ops.lineage_edges` | 376 |

| Team | Datasets owned |
|---|---:|
| platform | 36 |
| product | 27 |
| sales | 26 |
| marketing | 26 |
| finance | 26 |

11 datasets are critical, 129 important and 1 best effort. The best-effort one is `finance_fct_revenue_v0`, an abandoned model whose name still matches the critical revenue rule and which an exact rule demotes. The platform team owns more datasets than any business team, and all of them sit upstream of everything else, which is the load Phase 3 is expected to measure.
