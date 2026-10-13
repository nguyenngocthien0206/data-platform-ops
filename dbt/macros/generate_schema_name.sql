{#
  Use the configured schema name as is (staging, intermediate, marts) rather
  than dbt's default of prefixing it with the target schema. Query history in
  the cost module then shows the same names people use in conversation. In
  ClickHouse a schema is a database. The `namespace` var prefixes every one of
  them, so a test builds the project beside the demo without touching it.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ var('namespace', '') }}{{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
