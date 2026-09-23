# metadata

The shared layer every other module reasons about: who owns each dataset, and how data flows between them. Cost attribution uses it to send a bill to the right team and to avoid calling a table unused when it feeds one that is used. Incident management uses it to find the root cause of a cascade of failures and to route the incident to the person who can fix it.

## Ownership registry (`registry.py`, `check.py`)

`config/teams.yaml` lists five teams and their members: the four business teams (sales, marketing, finance, product) and a data platform team. `config/ownership.yaml` maps glob patterns on dbt unique ids to an owner, a team and a tier (`critical`, `important` or `best_effort`). It is CODEOWNERS for data.

When several rules match, the most specific one wins: the most literal characters, then the fewest wildcards. Two rules that tie are an error, never a coin toss decided by line order, because a reorder in a pull request should not be able to hand a critical dataset to a different on-call rotation. The full rule and the reasoning behind the platform team are in [ADR 0002](../../../docs/adr/0002-ownership-resolution-and-platform-team.md).

`platform-ops metadata check` fails when any model, source or exposure has no owner, when ownership is ambiguous, when a rule names an unknown team or an owner who is not on that team, or when a dashboard's owner in dbt disagrees with the registry. It warns about rules that match nothing. On success it writes the resolved owner of every dataset to `ops.node_ownership`, so later modules join on ownership in SQL. With `--parse` it builds the dbt manifest first, which needs no data, so the check runs in CI.

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
