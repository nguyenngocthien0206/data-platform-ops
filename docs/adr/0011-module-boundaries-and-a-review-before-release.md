# 0011: Module boundaries pinned by a test, and a review before the release

Status: accepted
Date: 2026-09-28

## Context

Phases 0 to 5 were built one after another, each under the time budget of its day. That left the usual residue: the incidents and reconcile reports borrowed the cost module's Markdown table helper, `simulation` and `reconcile` imported each other, a determinism primitive lived inside the workload generator, and the same "replace an `ops` table" and "write a throwaway config" code existed in several places. None of it was wrong on its own. Together it meant the three modules, which the design says are independent (ADR 0010), were quietly coupled, so a team taking over one module would inherit parts of the other two.

A release is the moment that matters, because after it the code has readers who did not write it.

## Decision

Before the release, one phase was spent reviewing the whole repository, under a single rule: no behaviour change. A clean `make demo` had to write byte-identical reports before and after, and any bug whose fix would move a reported number was to be raised with the owner rather than fixed on the way.

- **Boundaries are a test, not a convention.** `tests/test_architecture.py` parses every import in `src/` (including imports inside functions, which is how boundaries usually get crossed) and fails if a package imports one it may not. `cost`, `incidents` and `reconcile` never import each other. The one accepted exception is written down in the test: the simulated warehouse carries the cost module's collection instruments, the way a real warehouse carries its own query history.
- **Shared helpers live in `common`**: Markdown tables, replacing an `ops` table in one transaction, a stable hash, and an isolated config for tests and fixture generation.
- **A broader lint rule set stays on** (`RUF`, `PERF`, `PIE`, `RET`, `C4`, `PTH` on top of the basics), so the cleanup does not erode.
- **Every finding is recorded with a decision** (fixed, accepted with a reason, or deferred) in `docs/review/phase-6-review.md`: 22 findings.
- **Expensive test warehouses are shared.** One session-scoped warehouse, built once by all four modules at scale 0.01, serves every integration test that only reads it.

## Consequences

The reports stayed byte-identical, `make readme-check` kept passing, and the test suite went from 9 minutes 50 seconds to 6 minutes 24 seconds on the same laptop. The 5-minute target was missed: the determinism tests need independent runs, and that is the floor. The review record states it rather than hiding it.

For whoever owns a module next, the boundary test is the useful part: it turns "please do not import across modules" from a review comment into a failing check, so the rule survives people who never read this ADR. The cost is that the one exception has to be defended in writing, which is the point.
