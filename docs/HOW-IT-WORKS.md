# How it works

Each module of the toolkit, explained the same way: start from the simplest thing that could work, see where it breaks, and add one idea at a time, each one there to fix exactly one problem. If a step looks complicated, the problem just before it is the reason it exists.

The module READMEs have the numbers and the "Adopting it" guides; the [ADRs](adr/README.md) have the full reasoning. This page is the shortest path to understanding the mechanisms.

- [The company: data that repeats itself exactly](#the-company-data-that-repeats-itself-exactly)
- [Copying from the old database without a change stream](#copying-from-the-old-database-without-a-change-stream)
- [Ownership and lineage: who owns what, and what depends on what](#ownership-and-lineage-who-owns-what-and-what-depends-on-what)
- [Reconcile: proving the new system shows the same numbers](#reconcile-proving-the-new-system-shows-the-same-numbers)
- [Cost: splitting one bill fairly between teams](#cost-splitting-one-bill-fairly-between-teams)
- [Incidents: one page per problem, to the right person](#incidents-one-page-per-problem-to-the-right-person)
- [Schedulers: whatever runs dbt can feed the desk](#schedulers-whatever-runs-dbt-can-feed-the-desk)
- [Dashboards: showing results without touching production](#dashboards-showing-results-without-touching-production)

---

## The company: data that repeats itself exactly

**The question.** The toolkit needs a company to run on: five teams, a year of orders, payments and sessions, a legacy database, a new warehouse. And every number it produces must come out the same on every run, or nobody can check a cost or a sign-off.

**Step 0: generate random data.** Fill the tables with random customers and orders. It looks real, but every run gives different data, so every report changes, and a reviewer cannot reproduce anything.

**Step 1: use a fixed seed.** Seed the random generator once. Better, but fragile: generate the tables in a different order, or add one column, and every value after it shifts.

**Step 2: make every value a hash of where it lives.** Each "random" value is computed from `(seed, table, row id, column)` through a hash function. A row's value never depends on what was generated before it, so adding a column or a table changes nothing else.

**Step 3: run on a simulated clock.** Real time would make "yesterday's data" a different set on every run, and freshness checks would depend on the hour the demo was started. The company lives on its own clock: 112 days that start on 2026-01-05, replayed in under an hour.

**Step 4: remove the warehouse's own randomness.** Even with identical data, a warehouse can read a different number of bytes depending on how far its background merges have got, or on how many threads a query used. So the raw layer is merged every night, inserts are written in one part per partition, and workload queries run on one thread.

**The result.** Two runs from a clean state leave 195 of 196 relations identical (the one difference is the run's own random id) and every report byte for byte. That is what lets a number be quoted and checked.

**In three lines.** Every value is a hash of its position, time is simulated, and the warehouse is kept from varying on its own. Two clean runs are compared on every release.

**Where to look.** `src/platform_ops/simulation/raw_data.py`, `common/clock.py`, `simulation/company.py` (`optimize_raw`), `dbt/profiles.yml`.

---

## Copying from the old database without a change stream

**The question.** Every night, rows that are new or changed in the legacy database have to reach the new warehouse. The legacy database has no change stream; it only has an "updated at" column on every table.

**Step 0: copy everything every night.** Simple and always correct, but it reads every table in full every night, and the copy grows slower as the data grows.

**Step 1: copy only what changed since last time.** Remember how far the last copy read (the **watermark**), and next time read only rows whose "updated at" is after it.

**Step 2: catch the late commits.** A transaction that started at 01:59 and committed at 02:01 carries "updated at = 01:59", after the 02:00 copy read up to 02:00. It falls behind the watermark and is lost forever. So each copy re-reads a margin before the watermark (one hour), which catches anything that committed late.

**Step 3: make reading a row twice harmless.** The margin means some rows are read again, and a row changed in place arrives as a second version. The raw tables keep only the latest version of each key, so a duplicate read or an update simply replaces the old row.

**Step 4: record every load.** Each load writes its range and its row count to the ops store; the latest end of range is the next watermark. A broken table is fixed by copying it again in full from the source of truth.

**The limit.** A row that commits later than the margin is still lost. That is the limit of a watermark without a change stream, and why the margin is a setting tuned to the longest transaction.

**In three lines.** Read only rows past the watermark, minus a safety margin. Keep the latest version per key so re-reads are harmless. Record each load to know the next watermark.

**Where to look.** `src/platform_ops/simulation/ingest.py` (`Ingestion.load`, `watermarks`, `resync`), [ADR 0012](adr/0012-clickhouse-modelling-and-watermark-ingestion.md).

---

## Ownership and lineage: who owns what, and what depends on what

**The question.** For each of about 140 datasets: who answers for it, and what breaks if it breaks? Every other module needs both answers: cost to send a bill, incidents to page someone, reconcile to explain a report difference.

**Step 0: a spreadsheet of owners.** One row per dataset. Out of date within a month: every new model needs a new row, and nobody notices a missing one.

**Step 1: rules instead of rows.** Owners are given by patterns: "every `finance_*` model belongs to finance, owner bruno". One rule covers a whole folder, and a new model is owned the day it is created.

**Step 2: decide between overlapping rules.** A model can match "every mart" and "every `finance_*` model". The **most specific** rule wins: the one with the most literal characters, then the fewest wildcards. If two rules tie exactly, that is an error, not a coin toss: reordering lines in a pull request must never hand a critical dataset to a different on-call person.

**Step 3: check it where code is reviewed.** On every pull request, CI reads the dbt project's description of itself (its manifest: every dataset and its parents) and fails if any dataset has no owner, two owners, an unknown team, or an owner who is not on that team. Every finding names the file to edit, with a rule ready to paste.

**Step 4: start a large project without a blank page.** A starter registry is generated from the manifest, one rule per group of datasets, every owner set to `TODO`. Coverage starts at 0% on purpose. The check runs in report-only mode while the team closes the to-dos, then becomes required.

**Step 5: the lineage graph.** From the same manifest, every parent-to-child link becomes an edge in a graph. Three questions are then cheap: what is upstream of this node, what is downstream of it, and which failed node is closest above this one.

**In three lines.** Ownership is a set of rules where the most specific wins and ties are errors. CI blocks any dataset without exactly one owner. The manifest also gives the graph every other module walks.

**Where to look.** `src/platform_ops/metadata/registry.py` (`specificity`, `resolve`), `check.py`, `bootstrap.py`, `lineage.py`, [ADR 0001](adr/0001-ownership-resolution-and-platform-team.md), [ADR 0011](adr/0011-the-ownership-gate-in-the-consuming-repository.md).

---

## Reconcile: proving the new system shows the same numbers

**The question.** Before the old system is switched off, someone has to sign that the new one holds the same data and shows the same report numbers, and keeps doing so night after night. Here: 9 tables and 12 reports, every night for 42 nights.

**Step 0: pull everything and compare.**

```
legacy:  SELECT * FROM payments   → 476,961 rows
new:     SELECT * FROM payments   → 476,961 rows
match on payment_id, compare every column
```

Correct, but heavy: across 21 pairs that is about 5 million rows from each side every night, mostly to learn that nothing changed.

**Step 1: compare a summary instead of the rows.** Ask each side to summarise its table into a few numbers. The simplest summary is the row count, but on the first night every payment had a wrong time zone and the counts still matched. A count does not see the content.

**Step 2: turn each row into a number with a hash.** A hash function turns any text into a number. The same text always gives the same number on every system, and changing one character gives a completely different one (the numbers below are made up to show the idea):

```
"1000591|card|149.92|2025-07-04 13:11:49"  →  3,912,004,117
"1000591|card|149.91|2025-07-04 13:11:49"  →    208,551,930   (one cent apart)
```

Each side now returns two numbers, the row count and the sum of the row hashes. Equal counts and equal sums mean equal tables; a single different row changes the sum.

One condition: the same value must be written the same way on both sides before hashing. One database prints `149.9`, the other `149.90`, and their hashes would differ although the values are equal. So both sides first write every value in one **canonical** form: decimals at a fixed scale, times in UTC with microseconds, a special marker for NULL.

**Step 3: find where it differs, without pulling everything.** A different sum says the table differs, but not where. So the table is split into groups, with a count and a sum per group, and only a group that differs is pulled:

```
            group 0   group 1   group 2   group 3
legacy:     (2, 517)  (2, 902)  (2, 133)  (2, 760)     ← (rows, hash sum)
new:        (2, 517)  (2, 902)  (2, 488)  (2, 760)
                                  ↑ only group 2 differs: pull its 2 rows
```

**Step 4: group by the hash of the key.** A row must land in the same group on both sides, whatever order each database stores it in. Key ranges would work for numeric keys only, and spread unevenly. Hashing the key works for any key (a number, a date, three columns), spreads rows evenly, and puts the same key in the same group everywhere. The first hex digit of the key's hash gives 16 groups.

**Step 5: split differing groups again.** With 476,961 rows, a group holds about 30,000. So a differing group is split again by the next hex digit (256 groups, then 4,096...), asking only about the groups that still differ, until a group holds about 256 rows. Only then are real rows pulled and compared cell by cell.

```
level 1:  16 groups         → "7" differs
level 2:  "70" to "7f"      → only "7a" differs
level 3:  "7a0" to "7af"    → only "7a3" differs, about 230 rows: pull them
```

Why two hash numbers per row (`h1`, `h2`) in the code: two different groups could, by a tiny chance, have the same sum. Comparing two independent sums makes that chance negligible.

**Step 6: label each difference with a likely cause.** Finding a difference is not enough; the team needs to know who fixes it.

| Rule | Label | Real example (first night) |
|---|---|---|
| key only on the legacy side | missing | 74 web sessions lost at page boundaries |
| decimals within 0.01 | rounding | `149.92` became `149.91` |
| times a whole number of hours apart | time zone shift | `13:11:49` became `17:11:49` |
| equal once spaces are trimmed | whitespace | `"grace.novak53426@post.test "` lost its trailing space |
| equal once lower-cased | case only | |
| anything else | value mismatch | |

**Step 7: judge each pair against rules fixed in advance.** The data owner signs the rules before the run: whether trailing spaces count, the rounding tolerance, the minimum match rates, how many of each label are allowed. A pair passes or fails against them; the rules are printed first in the sign-off.

**Step 8: a report that differs needs a reason.** If the new revenue report differs from the old one, either a table it is built from differs too (a copy problem, fixed upstream), or nothing upstream differs and the new report computes differently (a logic problem). The lineage graph says which tables feed each report. A difference with no upstream cause blocks the night, because it is exactly what a finance user finds after the cutover.

**Step 9: count clean nights.** A night is clean when every pair passes and no report differs without a reason. After 14 clean nights in a row, the cutover is recommended.

**The result.** On matching nights the whole run pulled 11,200 rows instead of 378,885,458. Every defect of the delivered copy job was found on the first night, every one-off fault the night it landed.

**In three lines.** Write values canonically and hash each row. Group rows by the hash of their key and compare only counts and sums per group. Split differing groups until they are small, then pull and label the rows.

**Where to look.** `config/reconcile.yaml`, `src/platform_ops/reconcile/canonical.py` (steps 2 and 4), `connectors.py` (the per-group summaries), `diff.py` (steps 3 and 5), `classify.py` (step 6), `metrics.py` (step 7), `parallel.py` (steps 8 and 9), [ADR 0006](adr/0006-cross-engine-hashing-and-canonicalization.md), [ADR 0014](adr/0014-the-parallel-run-and-the-cutover-rule.md).

---

## Cost: splitting one bill fairly between teams

**The question.** The warehouse is a few machines paid by the month: one invoice, nothing per query. Who should see which part of it?

**Step 0: the platform team pays everything.** It is the team that runs the cluster, so the bill lands there. Nobody else sees what their habits cost, so nothing changes.

**Step 1: know who ran each query.** Every team, the BI tool, dbt and the loader each query as their **own user**, so the warehouse's query log records who ran every query and how many bytes it read.

**Step 2: know what each query was for.** "The dbt user" or "the BI user" is not an answer: whose model, whose dashboard? So every query carries a small tag: the kind of work (build, test, load, merge, reconciliation, dashboard, ad hoc) and its subject (a model, a dashboard, a person).

**Step 3: keep only what a warehouse would bill.** Statements that only change the catalogue (`CREATE`, `DROP`) or only read system tables are dropped, literal values in queries are masked, and queries are numbered in a stable order so two runs give the same ids.

**Step 4: split the invoice exactly.** Each month's stated cost is split in two pools. Compute is shared by the bytes each query read; storage by each table's size at the month's end. Shares are rounded very finely and the remainder goes to the largest share, so the parts add up to the invoice to the cent and finance can check them.

**Step 5: charge each query once, to the right team.**

- Building, testing, loading or merging a table is **production** cost, charged to the table's owner.
- Reading a table is **consumption** cost, charged to the reader: an ad hoc query to the person's team, a dashboard refresh to the team that owns the dashboard (the BI user only runs it for them).
- The migration's reconciliation is **migration** cost, carried by the platform team and shown on its own.

**Step 6: price the same work elsewhere.** The same measured queries are also priced as if on BigQuery (per byte scanned, with a minimum per table) and on Snowflake (per second a warehouse is awake, including the idle minutes after each query). The two models reward opposite habits, which is the point of showing both.

**Step 7: point at what to change.** A table is unused when nothing reads it or anything downstream of it; a hotspot is a table filtered again and again on a column it is not sorted by; quotas are suggested at twice a user's busiest day.

**Step 8: the business case.** The old reporting system's monthly cost against the new cluster's, both running until the cutover date reconcile recommends, and how long the saving takes to pay that back.

**The result.** The 112 days cost $1,197.24, split exactly. 844 ad hoc queries took $148.51 of it and 12,544 dashboard refreshes $3.34; on Snowflake the dashboards would be the expensive part. The migration pays for itself in 3.5 months.

**In three lines.** A user per team says who, a tag on every query says what for. The fixed invoice is shared by bytes read and stored, to the cent. Building is charged to the owner, reading to the reader.

**Where to look.** `src/platform_ops/simulation/access.py` (step 1), `common/tagging.py` (step 2), `cost/collect.py` (step 3), `cost/cluster.py` (step 4), `cost/attribution.py` (step 5), `cost/pricing.py` (step 6), `cost/recommend.py` (step 7), `cost/business.py` (step 8), [ADR 0013](adr/0013-users-per-team-and-bi-from-exposures.md), [ADR 0015](adr/0015-finops-on-a-self-managed-clickhouse.md).

---

## Incidents: one page per problem, to the right person

**The question.** When a raw table breaks, a dozen dbt tests fail below it. Who should be woken up, and how many times?

**Step 0: one alert per failing test.** Twelve failing tests, twelve pages, sent to whoever owns each failing model. In the production weeks this would have paged one engineer 28 times for two problems, neither of them theirs.

**Step 1: read every dbt result as soon as it is written.** After each dbt invocation (a night's freshness check, build, snapshot and test, or each task of a scheduler), its result files are handed to the desk with the id of the run they belong to. Nothing scans afterwards; the desk reacts as each step finishes.

**Step 2: find the root.** On the lineage graph, a failed node whose ancestors are all healthy is a **root**. Every other failed node belongs to the nearest failed node above it, and so up to a root. A node skipped because its parent failed is impact, not an alert.

For example:

```
raw orders (bad)  →  stg_orders  ✗ not_null fails      ← root
                         ├─ fct_orders     ✗ test fails     → belongs to stg_orders
                         └─ dim_customers  ✗ test fails     → belongs to stg_orders
                                └─ dashboards (skipped)      → impact
```

**Step 3: regroup the whole run every time.** A scheduler may deliver a run as 70 separate pieces, in any order. So each time a piece arrives, the desk regroups **everything** it holds for that run. Whatever the split and the order, the answer is the same: one incident per root.

**Step 4: compare with incidents already open.** The same root failing again the next night appends to its open incident instead of paging again. A new failure above an open incident's root, such as a feed that stopped above a table already broken, is absorbed into it. An incident opened in this run that turns out not to be the real root is merged into the real one.

**Step 5: score it.** The score adds up what sits downstream of the root, each model and dashboard weighted by how important it is (critical 3, important 2, best effort 1), plus the root's own importance. A break near the sources that feeds many critical dashboards is a SEV1.

**Step 6: page the person who can fix it.** By default, the owner of the root. One exception: a staging model reads one source and only renames columns, so when it fails, the data arrived broken, and the page goes to the **owner of the source**. The desk also computes the naive choice, so the value of the exception is measured: right 8 times out of 8, against 3.

**Step 7: write first, send after.** The page, every update and the resolution are written in the same transaction as the incident and sent only after it commits, so no page is ever sent for an incident the store does not hold. A chat that is down is recorded, never allowed to break the desk.

**Step 8: resolve only on evidence.** An open incident resolves when a later run covered its root and found it healthy: the model built and every test on it that ran passed, or the source is fresh again. A run that never touched the root cannot close it.

**The result.** 34 failing checks became 8 incidents and 8 pages, each to the right person. The numbers also showed what tools cannot fix: 6 of the 8 pages went to the one person who owns most feeds, and nothing closed in under 24 hours because resolution waits for the next nightly run.

**In three lines.** Group failures by their root on the lineage graph, regrouping the whole run on every piece. Page the root's owner once, the source's owner when a staging model breaks. Close only when a later run shows the root healthy.

**Where to look.** `src/platform_ops/incidents/desk.py` (`ingest_target`, `_regroup`), `grouping.py` (step 2), `severity.py` (step 5), `routing.py` (step 6), `notify.py` (step 7), `lifecycle.py` (the simulated owners), [ADR 0005](adr/0005-incident-grouping-severity-and-routing.md), [ADR 0016](adr/0016-incidents-on-the-companys-production-runs.md).

---

## Schedulers: whatever runs dbt can feed the desk

**The question.** Teams run dbt from Airflow, from Dagster or from a cron line. The incident desk has to work with all of them without becoming part of any.

**Step 0: import the desk into each scheduler.** Call the desk's Python code from the scheduler's own process. Now the scheduler's packages, the toolkit's packages and dbt's packages must all agree on versions in one environment, and upgrading any of them can break the others.

**Step 1: call a command instead of a library.** The desk is a command: `platform-ops incidents ingest --target <folder> --run-id <id>`. The scheduler only runs a command with two arguments; the toolkit lives in its own environment, and nothing has to agree on versions.

**Step 2: one run id per scheduled run.** Airflow with Cosmos runs one dbt invocation per model, about 70 of them in a faulted night; Dagster runs one build. Every invocation of one scheduled run is handed over with the same id (`airflow:<dag>:<run>`, `dagster:<run>`), and the desk regroups them as one run, so both give the same single incident.

**Step 3: hand over failed runs too.** The interesting runs are the failed ones. The hook runs after every task whether dbt passed or failed, while the task's result folder still exists.

**Step 4: never fail the pipeline.** If the hand-over fails or hangs, it is logged and dropped. Incident tracking that breaks the company's data pipeline would be worse than none.

**In three lines.** Schedulers call a command with a folder and a run id. All invocations of one run share the id and are regrouped as one. A failed hand-over never fails the pipeline.

**Where to look.** `src/platform_ops/integrations/` (the hooks), `orchestrators/airflow/dags/company_dbt.py`, `orchestrators/dagster/app/company_dbt.py`, `scripts/check_orchestrators.py` (the end-to-end check), [ADR 0017](adr/0017-orchestrators-call-the-toolkit.md).

---

## Dashboards: showing results without touching production

**The question.** People would rather look at the results than read Markdown. How do you give them pages without giving them a way to break or overload anything?

**Step 0: let the pages query the warehouse.** Quick to build, but now a page can run any query against production data, as whatever user it connects with, and recompute numbers its own way.

**Step 1: one store of results.** Every module writes its results to one separate database, the ops store. The pages read only that, never the company's data.

**Step 2: a read-only role of their own.** The pages connect as a role that can read the ops store and nothing else, cannot write even if a session asks to, and keeps its access to tables every run drops and recreates. Nobody reads as the admin.

**Step 3: one data layer, tested.** Every query the pages run sits in one file with the tables it needs, and a test parses each query and fails if it reads anything outside the ops store.

**Step 4: nothing recomputed in a page.** Whatever a report states is stored by the module that computed it; the page only shows it. A page can never disagree with a report.

**Step 5: check the pages in a real browser.** After a run, a headless browser opens every page in both themes and fails on any error, saving a screenshot of each. The screenshots found what no assertion would: a caption that turned two dollar amounts into a formula, and labels a chart silently dropped.

**In three lines.** Pages read only the ops store, as a read-only role. Every query is in one tested file. Nothing is recomputed, and every page is opened in a browser after a run.

**Where to look.** `dashboards/`, `src/platform_ops/dashboard/queries.py`, `charts.py`, `scripts/check_dashboards.py`, [ADR 0013](adr/0013-users-per-team-and-bi-from-exposures.md).
