# reconcile

Before a legacy system is switched off, someone who owns the data has to sign that the new copy holds what the old one held. `make reconcile` produces the evidence for that signature: it loads a year of legacy orders, customers and payments into Postgres, migrates them into Iceberg with a job that has realistic bugs, plants a few more discrepancies in the target, then finds and explains every difference with a segmented checksum diff. The result is `reports/reconciliation.md`, a sign-off document that states its acceptance thresholds before it states any numbers.

## How it fits together

| Step | Module | Output |
|---|---|---|
| Describe | `schema.py`: the three legacy tables, their DDL per engine and their logical column kinds | |
| Generate | `legacy.py`: a year of legacy data with local timestamps, padded text, high-precision decimals, NULLs next to keys | legacy tables in Postgres |
| Migrate | `migrate.py`: export, transform in DuckDB, write to Iceberg (PyIceberg, SQLite catalog) with four defects | Iceberg tables, ground truth |
| Break more | `simulation/migration_faults.py`: seven labelled one-off faults in the target | ground truth |
| Render | `canonical.py`: one canonical string per value and one row hash, rendered identically by Postgres, SQL Server, DuckDB and Python | |
| Connect | `connectors.py`: `SourceConnector` for Postgres, SQL Server and DuckDB (Iceberg is read through DuckDB) | |
| Diff | `diff.py`: segment summaries level by level, rows fetched only for differing leaves | `ops.reconcile_segments` |
| Classify | `classify.py`: missing, extra, rounding, time zone shift, whitespace, case, value mismatch | `ops.reconcile_discrepancies` |
| Judge | `metrics.py`: match rates, verdict against thresholds, grading against ground truth | `ops.reconcile_tables`, `ops.reconcile_metrics`, `ops.reconcile_ground_truth` |
| Report | `report.py` | `reports/reconciliation.md` |

Nothing in the diff reads the ground truth. Only the grading does, the same split as the incident module.

## The decisions that shape the numbers

**One canonical string per value, in every engine.** A checksum diff only works if two engines hash the same data the same way, and they disagree on almost everything: decimal padding, boolean spelling, time zones, text encoding. Every value is rendered first (decimals at fixed scale, timestamps as UTC with microseconds, a NULL sentinel that never equals an empty string) and the row is hashed as MD5 of its UTF-8 bytes, summed per segment as two 32-bit halves. A golden-row test holds Python, DuckDB, Postgres and SQL Server to byte-identical output (ADR 0009).

**Equality is a policy the data owner signs.** Whether trailing spaces or letter case count as differences is not a technical question. The rules live in `settings.yaml` per engine and column, both sides use the legacy engine's rules, and the report prints them. SQL Server folds case because the business has been living with its case-insensitive collation for years; Postgres does not.

**Two passes, because that is how sign-off really goes.** The first pass diffs the job as delivered. It fails, and the systematic defects it finds are a message for the migration team, not for the reconciliation: fix the job. The second pass re-runs the migration with the defects fixed, leaving only the planted one-off faults, which is when a reviewer can actually read every remaining difference.

**Only differing segments are opened.** Keys are split into ranges, each engine returns a count and two hash sums per range, and only ranges that disagree are split again, 16 ways, down to 256 keys. Each engine hashes its rows once into a temporary table and groups that at every level, which took the diff from 53 to 20 seconds at scale 1.0.

## Results at scale 1.0

From two actual runs of `make reconcile` (50,000 customers, 300,000 orders, 300,000 payments), which wrote byte-identical reports in about 52 seconds each. The full `make demo`, all four modules from a clean state, took 6 minutes 54 seconds.

**Verdict: not signed off.** Both passes fail the thresholds, which allow no missing, extra, shifted or mismatched values.

| Pass | Table | Rows with differences | Missing | Extra | Row match |
|---|---|---:|---:|---:|---:|
| As delivered | customers | 2,510 | 6 | 0 | 94.968% |
| As delivered | orders | 1,440 | 12 | 5 | 99.516% |
| As delivered | payments | 195,492 | 0 | 0 | 34.836% |
| Job fixed | customers | 13 | 6 | 0 | 99.962% |
| Job fixed | orders | 6 | 0 | 5 | 99.998% |
| Job fixed | payments | 14 | 0 | 0 | 99.995% |

**Detection against ground truth.** Every one of the 199,465 planted discrepancies in the first pass, and all 44 in the second, was found and put in the right class: 100% recall, 100% precision, 100% classification accuracy. Each job defect was found in full: the daylight saving bug in 195,478 payments, the float cast in 1,434 large orders, trimmed padding in 2,497 company names, and the 12 orders lost at batch boundaries.

| Pass | Table | Rows moved, segmented | Rows moved, naive | Saving |
|---|---|---:|---:|---:|
| As delivered | customers | 100,412 | 99,994 | -0.418% |
| As delivered | orders | 434,047 | 599,993 | 27.658% |
| As delivered | payments | 602,502 | 600,000 | -0.417% |
| Job fixed | customers | 9,556 | 99,994 | 90.443% |
| Job fixed | orders | 3,851 | 600,005 | 99.358% |
| Job fixed | payments | 7,742 | 600,000 | 98.710% |

What the numbers say:

- **A systematic defect makes the smart diff no smarter than a dumb one.** Trimmed padding touches 5% of customers, which lands in every 256-key segment; the daylight saving bug touches 65% of payments. In the first pass the diff moves as many customer and payment rows as a full comparison would, and saves only 28% on orders; every top-level segment of customers and payments already differs, so the first level says as much. That is the cue to stop diffing and send the job back.
- **Once the job is fixed, the diff pays for itself.** With only a few dozen one-off faults left, it moves between 0.6% and 9.6% of what a full comparison would, which is what makes a nightly verification of a large migration affordable instead of a one-off before cutover.
- **The float cast is the quiet one.** Ordinary amounts survive a trip through DOUBLE; only the 0.5% of orders above ten billion lose their sixth decimal. That still adds up to 1,434 rounding differences, over the threshold of 100, so orders fails on them as well as on the rows dropped at batch boundaries. Whether a sixth-decimal difference on an enterprise invoice is acceptable is a call for finance, and the threshold is where they make it.

## Running it

```bash
make up                             # Postgres, the default legacy engine
make reconcile                      # both passes, ops tables, reports/reconciliation.md
uv run platform-ops reconcile run --strict   # exits non-zero when not signed off, for CI
```

SQL Server is optional. Start it with `docker compose --profile sqlserver up -d`, install the driver with `uv sync --extra sqlserver`, then run `uv run --extra sqlserver platform-ops reconcile run --engine sqlserver`. The same scenario runs against it; its case-insensitive policy reports the planted upper-case emails as equal under the source's policy rather than as discrepancies.

Tunables live under `reconcile:` in `config/settings.yaml`: legacy row counts, the legacy time zone, fanout and leaf width, the rounding tolerance, the canonical rules and the sign-off thresholds.
