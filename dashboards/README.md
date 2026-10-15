# dashboards

One Streamlit app with a page per module, for people who would rather look at the results than read Markdown. It reads only the ops store in Postgres that `make demo` fills, as its own read-only role, never the company's data, and writes nothing. The company's own dashboards are a different thing: they are in Metabase (`make metabase`).

```bash
make dashboard            # opens http://localhost:8501 in your browser
uv run platform-ops dashboard --headless --port 8600   # no browser, another port
make docker-dashboard     # served from the container on http://localhost:8501
```

| Page | What it answers |
|---|---|
| Overview | The company's run (window, rows ingested, lineage), one headline per module (the cluster's cost, the saving and payback, the cutover and what the parallel run found), and who owns which datasets |
| Cost | The cluster's stated cost shared out by team and by month, or the same workload priced on BigQuery or Snowflake; cost by kind of work; the migration's business case month by month; and what to change: unused tables, full-scan hotspots with a suggested key, incremental candidates, quotas with their SQL |
| Incidents | How many failing checks became how many incidents in the production weeks, pages per person before and after grouping, time to detect and resolve by severity, every incident from the run that opened it to the run that resolved it, and the nights the desk ingested |
| Reconciliation | The cutover verdict; a grid of the 42 nights of the parallel run against the 21 compared tables and reports; one night in detail (what was planted and found, what differed and why, examples); and rows moved by the segmented diff against a naive comparison |

## How it is built

- **Its own role.** The pages connect as `dashboard`, a Postgres role that `make demo` creates with nothing but `CONNECT` on the ops database, `USAGE` on the ops schema and `SELECT` on its tables, including the tables every run drops and recreates (default privileges for the toolkit's user, which creates them). Its sessions are read-only by default, and it has no privilege to write even when a session turns that off. Its password comes from `POSTGRES_PASSWORD_DASHBOARD` in `.env`, with no default; without it `make demo` stops before writing anything and `make dashboard` exits with code 2 and names the variable. It is the same rule as the company's ClickHouse users: nobody reads as the admin ([ADR 0016](../docs/adr/0016-users-per-team-and-bi-from-exposures.md)).
- **One data layer.** `src/platform_ops/dashboard/queries.py` holds every query the pages run, each with the tables it needs, written against `{ops}` and filled in with the (namespaced) ops schema. A test parses each one and fails on any table outside `ops`. A missing table shows which command to run instead of an error. Each read opens and closes its own connection, and results are cached for 30 seconds, so the pages can be left open while a run writes the store.
- **Nothing recomputed.** The pages show what the modules stored. Everything `reports/cost.md` states beyond the per-query facts (the month pools, the business case, unused tables, hotspots, incremental candidates, quotas) is stored by `make cost` in its own ops table for this reason.
- **One set of colour rules.** `src/platform_ops/dashboard/charts.py` builds every chart. A team keeps the same colour on every chart whatever a filter hides; a single measure uses one hue; the series that matters is the accent and its baseline grey. Pass and fail on the reconciliation grid use fixed status colours, never a team's, and a failing cell also carries a glyph (✕ for a failure, ↑ for a report whose difference comes from a table upstream), so the grid reads without colour. Every chart has hover tooltips and its data table in an expander under it.
- **Thin pages.** `dashboards/pages/*.py` read frames and lay them out.
- `.streamlit/config.toml` at the repo root turns off Streamlit's usage statistics, because the toolkit runs offline, and binds to `localhost`; the container overrides the address so the published port answers.

## Checks

`tests/test_dashboard.py` renders every page with Streamlit's `AppTest` on the shared test company (three weeks at scale 0.01): titles, charts, no "run this first" notices, the pricing switch and the night picker. It also checks the role: it reads every table, a table recreated after the grant included, and is refused every write.

`make docker-browser-check` (after `make docker-demo`) loads every page in headless Chromium, in the light and the dark theme, fails on exception boxes, error alerts, failed requests, console errors and pages without a chart or a data table, and saves a screenshot of each to `reports/screenshots/` (`scripts/check_dashboards.py`).

Looking at those screenshots found what no assertion would have:

- two dollar amounts in one caption turned into a LaTeX formula;
- labels cut off on the workload chart and the reconciliation grid, where every other row label was dropped;
- dark dots where the rounded corners of four grid cells met.

All three are fixed. The check itself found one more thing: Streamlit reports a page as done for a moment between running `app.py` and running the page, which a cold first load could land on, so the check now waits until the page stays done. The 1.x limits it found earlier (repeated month labels and an unexplained drop at the end of the window, a timeline without dates, narrow tables cutting off a column) are gone: the monthly chart names each month with the days the window covers in it, and the incident timeline's axis shows dates.

## Adopting it

The pages read the tables the simulated company's run writes (`company_run`, the cost, incident and reconciliation tables), so they do not run against your platform as they are. What carries over is how they are built:

- **A read-only role of their own** on the results store, with default privileges so tables recreated by every run stay readable.
- **One data layer** (`queries.py`) holding every query with the tables it needs, tested to read nothing outside the store, so a page can never touch production data.
- **Nothing recomputed in a page:** whatever a report states is stored by the module that computed it, and the page only reads it.
- **One set of colour rules** for every chart (`charts.py`), and a status shown by a glyph as well as a colour.
- **A browser check with screenshots** after every run, which found what no assertion did.

To reuse them, point `queries.py` at your own results tables and keep the pages thin.
