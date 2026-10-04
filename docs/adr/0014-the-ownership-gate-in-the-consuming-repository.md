# 0014: The ownership gate runs in the consuming repository, on its manifest

Status: accepted
Date: 2026-10-04

## Context

Phase 10 takes the ownership check (ADR 0002) to the company's own dbt projects, starting with the data platform team's project on self-managed ClickHouse, whose CI is GitHub Actions. Until then the check only knew the bundled project: it loaded the toolkit's settings, found the manifest through the demo's dbt project, read the registry from the toolkit's `config/`, and wrote its result into the demo's DuckDB.

Three things had to be settled. Where the company's registry lives, given that this toolkit's repository is public. How the check gets a manifest for a project on an adapter the toolkit does not ship. And how a required check gets switched on in a repository where most models have no owner yet, without the team switching it off again the same week.

## Decision

- **The registry lives in the consuming repository.** Each company dbt repository keeps its `teams.yaml` and `ownership.yaml` next to the project (conventionally in `ownership/`). The toolkit's repository holds code and the demo only, so no company names, channels or chats are ever published. Ownership changes go through the same pull requests as the models they describe, reviewed by the same people.
- **The check reads a manifest; it does not run dbt.** The consuming CI runs `dbt parse` with its own adapter and a parse-only profile (parsing never connects), and passes `manifest.json` to the toolkit's public image with `--manifest`. The toolkit needs no adapter, no credential and no warehouse, so the same image serves ClickHouse today and Snowflake or BigQuery later. In that mode it loads no settings and writes nothing but an optional summary.
- **Every finding points at the file to change.** An unowned dataset comes with a ready-to-paste rule; a rule problem points at its line in `ownership.yaml`. With `--format github` each finding becomes an annotation on that file and line in the pull request.
- **Exit codes separate the author's problem from the setup's.** 0: every dataset has one valid owner. 1: owners to fix in the registry. 2: the check could not run (no manifest, an invalid registry, a bad option). A broken pipeline never looks like a missing owner, and the reverse.
- **Report-only comes before required.** `--report-only` prints every problem and exits 0, and `--summary-json` records the share of datasets with an owner per resource type. A team starts there, measures the baseline, completes its registry, then makes the job required.
- **Seeds and snapshots need owners too**, because company projects have them and someone answers for each.
- **The check is tested on a project it was not written for**: a small dbt project on ClickHouse (`tests/fixtures/dbt_clickhouse`), with its own layout, a local package, seeds, a snapshot and exposures, parsed in the tests and built for real on a ClickHouse service in Compose.

## Consequences

Adopting the check costs a team one workflow file, a registry and a few weeks in report-only mode, and needs no access request, because nothing touches the warehouse. The pilot gets a before-and-after number for free: the share of datasets with an owner on the day the job is added, and on the day it becomes required.

The price is that the registry is spread across repositories. A person who leaves has to be removed from every `teams.yaml` that names them, and the toolkit cannot see ownership across projects until the incidents and cost modules read these registries (Phases 11 and 12). The consuming CI also has to keep `dbt-core` and its adapter installable just to parse, which is usually already true for a dbt repository with any CI at all.
