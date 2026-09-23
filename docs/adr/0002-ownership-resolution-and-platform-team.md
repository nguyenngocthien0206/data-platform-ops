# 0002: Ownership resolution, and a platform team that owns everything upstream

Status: accepted
Date: 2026-09-23

## Context

Cost showback and incident routing both start with the same question: who owns this dataset? The registry answers it with glob rules on dbt unique ids, the way a CODEOWNERS file maps paths to reviewers. Globs overlap by design. A team claims `model.company.finance_*` as a default, and a narrower rule such as `model.company.finance_fct_revenue*` hands the revenue models to one person at a critical tier. Overlap needs a rule for which match wins, and the choice has consequences for people, not just for code. If the winner depends on where a line sits in the file, then a harmless-looking reorder in a pull request can move a critical dataset to another team's on-call rotation, and nobody notices until the first page goes to the wrong person.

The simulated company also needed an answer for who owns the layers below the marts. Raw sources, staging and intermediate models are shared by every team. The organisation could split them by business domain, where payments belong to finance and sessions to product, or it could give them to a central data platform team. The choice decides where incidents land, because most root causes surface upstream, at a source that stopped loading or a staging model that broke on a schema change.

## Decision

The most specific matching rule wins. Specificity is the number of literal characters in the pattern, with fewer wildcard tokens (`*`, `?`, `[...]`) breaking a tie. If the two best candidates are still equal, `platform-ops metadata check` fails and names both rules. Rules are never ranked by file order, and an exact duplicate pattern is rejected when the file is loaded. A rule may only name an owner who is listed as a member of the team on the same rule, so ownership cannot quietly point at someone who has moved teams. A rule that matches nothing produces a warning rather than a failure, so renaming a model is still a one-file change while the stale rule stays visible in the output.

A dedicated platform team owns every raw source, staging model and intermediate model. The four business teams own their marts and the dashboards built on them. The one place this rule does real work in the current project is `finance_fct_revenue_v0`: an abandoned model whose name still matches the critical revenue rule, demoted to best effort by an exact rule that is more specific than the glob.

## Consequences

Ownership is deterministic, and a reviewer can predict the owner of any dataset by reading the rules, without knowing their order. A tie is a conversation between two teams, which is the right place to settle it, instead of an accident in a diff. The check runs in CI without any data, because it only needs a `dbt parse` manifest. That makes it cheap enough to block a merge, which is what turns the registry from documentation into a control.

Centralising upstream ownership concentrates load. In the first build the platform team owns 34 of the 139 datasets, more than any business team, and those 34 sit upstream of almost everything else. Phase 3 should show the result directly: most incidents route to three people. That is a realistic picture of a central platform team, and it is also the argument such teams make for headcount or for pushing source ownership out to domain teams. The registry makes the trade-off measurable instead of anecdotal. If the organisation later moves to domain ownership, the change is a handful of rules in `config/ownership.yaml`, not a change to any code.
