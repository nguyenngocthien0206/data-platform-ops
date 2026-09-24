# 0009: Cross-engine hashing and canonicalization

Status: accepted
Date: 2026-09-24

## Context

A migration is signed off by the people who own the data, not by the people who wrote the job, and they need evidence that the target holds what the legacy system held. Comparing two engines row by row is the obvious evidence, and it scales badly: moving every row of a large table out of both engines costs time and network, so in practice it is done once, late, on a sample. A segmented checksum diff asks each engine for a count and a checksum per key range and only fetches rows where the checksums disagree. That only works if two engines produce the same checksum for the same data, and they do not agree on how to print a value: `2.3` against `2.30`, `t` against `true`, a local timestamp against its UTC instant, text as UTF-8 against UTF-16.

## Decision

**Canonical rendering.** Every value is rendered to one agreed string before hashing, by the same rule in Postgres, SQL Server, DuckDB and a Python reference:

- decimals at a fixed scale (the column's own by default), rounded half away from zero, with no negative zero;
- timestamps as UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`. A legacy local timestamp is first converted from the configured legacy zone (`America/New_York`, or `Eastern Standard Time` in SQL Server's naming);
- text optionally right-trimmed and optionally lower-cased, then `\` escaped as `\\` and `|` as `\|`;
- booleans as `true` or `false`;
- NULL as `\N`, which no escaped value can equal, so NULL never matches an empty string.

The rules are configured per engine and per column in `settings.yaml`. Both sides of a comparison use the legacy engine's rules, because the legacy system's idea of equality is what the business has been living with. SQL Server folds case, since its default collation is case-insensitive; Postgres does not. That is a sign-off decision written down before the run, not a fudge applied after it, and the report prints it.

**Hash.** A row is its canonical values joined by `|`, hashed with MD5 over its UTF-8 bytes. MD5 is available in all three engines (`md5()` and SQL Server's `HASHBYTES('MD5', ...)`); it is a checksum here, not a security control. The digest is reduced to two unsigned 32-bit integers, hex digits 1 to 8 and 9 to 16 (in SQL Server, bytes 1 to 4 and 5 to 8 converted to BIGINT). A segment is summarised as its row count and the sum of each integer. Sums rather than XOR because SQL Server has no XOR aggregate, and because a sum is additive, so a parent segment's summary is exactly the sum of its children's. Each row adds under 2^32, so SQL Server's BIGINT sum cannot overflow below about two billion rows in one segment; Postgres returns NUMERIC and DuckDB HUGEINT. SQL Server's legacy database uses a `_UTF8` collation, so its VARCHAR bytes, literals included, are UTF-8 like the others.

**Proof.** A golden-row test renders rows chosen to break naive schemes (non-ASCII names, a literal `\N`, pipes and backslashes in text, NULL next to an empty string, negative half-way decimals, a value that rounds to zero from below, local times on both sides of daylight saving) in Python, DuckDB, Postgres and SQL Server, and requires identical strings and identical hash sums. All four agree on the installed versions.

**Segmented diff.** Keys are split into ranges whose widths are the leaf width (256) times a power of the fanout (16), so levels nest exactly and each level is one `GROUP BY` query per side, whatever the number of segments. Only segments that differ are split again, and only differing leaves are fetched. Each side first hashes its rows once into a temporary key-and-hash table inside its own engine; every level then groups that small table. Canonical rendering is the expensive part, and a dense table is summarised over nearly every row at every level: this cut the diff from 53 to 20 seconds at scale 1.0. The Iceberg target is read by DuckDB from the Parquet files PyIceberg plans, which works offline; DuckDB's `iceberg` extension would need a download at setup.

**Two passes.** The job as delivered is diffed first. Its systematic defects touch most segments (a daylight saving bug shifts 65% of payments; trimmed padding touches 5% of customers, which is every 256-key leaf), so that pass moves about as many rows as a naive comparison. Then the job is re-run with its defects fixed, leaving only a few dozen one-off discrepancies, and the diff moves 90% to 99% fewer rows than a naive comparison.

## Consequences

The sign-off report can be trusted across engines because the hash agreement is tested, not assumed. The price is a canonical policy someone has to own. Whether trailing spaces or letter case matter is a business question, and the answer lives in config where a reviewer can see it; a planted case change that SQL Server's policy calls equal is reported as "equal under the source's policy", not as a miss.

The segmented diff is a verification tool, not a discovery tool. When a defect is systematic, the first level already shows every segment differing, and the useful action is to fix the job, not to diff harder. The report shows both passes so that finding is visible in numbers.

Local times inside the hour the clocks go back are ambiguous, and engines resolve that ambiguity differently. The legacy data avoids that hour; a real migration would need to declare which occurrence it means, per column, before signing off.

MD5 over row strings is not collision-free in principle, but a collision would have to hit both 32-bit sums of the same segment at once, and the leaf comparison uses the full canonical values, not the hashes.
