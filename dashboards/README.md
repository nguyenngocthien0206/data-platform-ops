# dashboards

One Streamlit app with a page per module, for people who would rather look at the results than read Markdown. It reads only the `ops` schema that `make demo` fills, never raw data or dbt models, and writes nothing.

```bash
make dashboard            # opens http://localhost:8501 in your browser
uv run platform-ops dashboard --headless --port 8600   # no browser, another port
```

| Page | What it answers |
|---|---|
| Overview | One headline per module, and who owns which datasets |
| Cost | Showback by team under either pricing model, the monthly trend, cost by workload, top models, unused tables, full-scan hotspots, incremental candidates |
| Incidents | How many failing checks became how many incidents, alerts per person before and after grouping, time to detect and resolve by severity, the incident timeline |
| Reconciliation | The sign-off verdict per table, detection against ground truth, discrepancies by class with samples, and rows moved by the segmented diff against a naive copy |

## How it is built

- `src/platform_ops/dashboard/queries.py` holds every query the pages run, each with the tables it needs. A test parses them and fails on any table outside `ops`. A missing table shows which `make` target to run instead of an error.
- `src/platform_ops/dashboard/charts.py` builds every chart, so colour follows one set of rules: a team keeps the same colour on every chart whatever a filter hides; a single measure uses one hue; before and after are two shades of one hue; the series that matters is the accent and its baseline is grey. The categorical slots were validated for colour-blind separation on Streamlit's light and dark surfaces. Three light-mode slots sit below 3:1 contrast, so every chart has hover tooltips and its data table in an expander right under it.
- `dashboards/pages/*.py` are thin: they read frames and lay them out.
- `.streamlit/config.toml` at the repo root turns off Streamlit's usage statistics, because the toolkit runs offline.

`tests/test_dashboard.py` builds a real warehouse at scale 0.01 with all four modules and renders every page with Streamlit's `AppTest`.
