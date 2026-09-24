# 0008: Incident grouping, severity and routing

Status: accepted
Date: 2026-09-24

## Context

One broken raw table fails many checks at once. A null spike in `raw.orders` fails the `not_null` test on `stg_orders`, then flows into every model built on orders and fails tests there too. If each failing check pages its node's owner, the people who own the marts get paged for a problem they cannot fix, and the person who can fix it gets paged once among many alerts. Phase 3 has to turn that storm into one incident per real problem, decide how bad it is, and page the one person who can act on it.

Three dbt behaviours shaped the design. `dbt build` marks everything below a failed test as skipped (`MARK_DEPENDENT_ERRORS_STATUSES` in dbt-core 1.12.5 includes `Fail`), so downstream checks never run and the storm never shows. `dbt run` followed by `dbt test` only skips below a model that errors, so bad data flows downstream and downstream tests fail as they would in production. And a relationships test depends on two models but is attached to only one of them.

## Decision

**Detection.** The scenario runs `dbt run` then `dbt test`, plus `dbt source freshness` on simulated time for stale sources. Failures are normalised into check events: a model that errors, a test that fails or errors, a source whose freshness errors. A freshness `warn` is not an alert. A skipped model is impact, not an alert.

**Subject of a check.** Every event is about one node, its subject: the model that errored, the stale source, or the model a test is attached to. A test that reads two models, such as a relationships test, is its own subject, and lineage places it below both models. Otherwise a volume drop in `raw.orders` would fail the relationships test attached to `stg_order_items` and open a second incident rooted at order items, which has nothing wrong with it.

**Grouping.** Within one run:

1. A node has failed if any check about it failed.
2. A failed node is a root if none of its ancestors failed. Every other failed node belongs to the root reached by following nearest failed ancestors up the graph. When two failed ancestors are equally close, as in a diamond, the smaller unique id wins, so the same failures always group the same way.
3. One incident per root. Several checks failing on one node are one incident.
4. If an incident for the same root is still open from an earlier run, the new failures are appended to it and the owner gets an update, not a page. An incident that recurs after it was resolved opens a new incident that points back to the old one.

**Severity.** `score = 10 x tier_weight(root) + sum of tier weights of every model and exposure downstream of the root`, with tier weights critical 3, important 2, best effort 1. The first term is how much the broken dataset matters on its own, and the multiplier of 10 puts it on the same scale as a large downstream. The second is how much of the business is built on it. Measured on this project's lineage, the downstream weights are 213 for customers, 189 for orders, 160 for order items and products, 76 for campaigns and web sessions, 34 for support tickets, 20 for marketing spend and 19 for payments. SEV1 starts at 150 and SEV2 at 45. With those thresholds anything rooted in orders or customers is SEV1, payments, support tickets, web sessions and campaigns are SEV2, and marketing spend is SEV3. The thresholds live in `settings.yaml` because they are a calibration against one lineage graph, not a universal rule. A team with a different graph should re-measure and move them.

**Routing.** The page goes to the owner of the root, except when the root is a staging model reading a single source. Then it goes to the owner of that source. Staging models only rename and cast, so a failure there almost always means the data arrived broken, and the person who can fix it owns the feed. The report grades both this rule and the naive one (page whoever owns the root) against ground truth.

**Lifecycle.** Incidents go from open to acknowledged to resolved. The time to acknowledge and the time to fix have a base per severity, stretched by `1 + 0.5 x` the number of incidents the owner already has open, and spread between 0.5x and 1.5x by a stable hash of the seed and incident id. Resolving an incident repairs every active fault upstream of its root.

**Running only what faults touch.** dbt runs for real only on nights with an active fault, on `@source:raw.<table>`: everything downstream of the faulted source plus every ancestor of those models. Downstream alone was tried first and found wrong. Every model is a table, so a parent the run did not rebuild keeps old data, and a relationships test between a fresh `stg_payments` and a stale `stg_orders` failed for no reason, opening two incidents nobody caused. The end-to-end test runs a full `dbt run` and `dbt test` on every fault night and checks that they find exactly the failing checks the targeted run found.

Two more choices keep the scenario inside its share of the 10-minute demo. The project is parsed once after the baseline build, and that parse is handed to every later dbt invocation, including source freshness: dbt resolves `simulated_now` when it runs a node, not when it parses, so freshness took 0.6 s instead of 5.2 s for a full re-parse. And faults land in pairs on different tables, so one targeted run covers two of them. The first full-scale run, with faults spread over seven different days and a re-parse for every freshness check, took 3 minutes 13 seconds; with both changes, three runs took between 2 minutes 22 and 2 minutes 56 seconds.

## Consequences

Every injected fault maps to exactly one incident, and the report shows how many raw alerts that replaced. Because the platform team owns every source, staging and intermediate model, every incident in the scenario pages a platform engineer. That is the concentration ADR 0002 predicted, and the lifecycle model turns it into a number: an owner with open incidents is slower on the next one.

The grouping is per run. Two unrelated faults that happen to fail the same downstream model in the same run attach that model to one of them by the tie-break, not to both. The root-cause answer is still right for each fault; only the impact list of one incident is shorter than it could be.

Severity rewards breadth. A critical dataset with nothing downstream scores 30, which is SEV3. That is intended for a demo about blast radius, but a team whose most critical tables are leaves should add a floor per tier.

Running only on fault nights means a check that would fail on a green night is never seen. The baseline build on day 0 has to be fully green or the scenario stops, which is what makes skipping green nights safe.
