# Legacy reporting

The reports the business reads today, written by hand in plain Postgres SQL on
the operational tables, the way the company built them before it had a
warehouse. One report per dashboard (dbt exposure), each the dashboard's main
table. Files starting with `_` are helper views the reports share.

`{app}` and `{reporting}` are replaced with the namespaced schema names when the
views are created. Phase 12 reconciles each report against the dbt mart that
replaces it on ClickHouse.
