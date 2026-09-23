{#
  Use the configured schema name as is (staging, intermediate, marts) rather
  than dbt's default of prefixing it with the target schema. Query history in
  the cost module then shows the same schema names people use in conversation.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
