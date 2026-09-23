{#
  Source freshness measured on simulated time.

  dbt's default compares max(loaded_at_field) with the warehouse's
  current_timestamp. This project's data lives in simulated time (the window
  opens in January 2026), so against the real clock every source would read as
  stale and the freshness alerts would mean nothing. Instead, "now" is the
  simulated clock, passed in by platform-ops as the `simulated_now` var.

  There is no fallback to the wall clock on purpose. A missing var fails loudly
  rather than producing alerts nobody should trust. See ADR 0003.
#}
{% macro duckdb__collect_freshness(source, loaded_at_field, filter) %}
  {%- set simulated_now = var('simulated_now', none) -%}
  {%- if simulated_now is none -%}
    {{ exceptions.raise_compiler_error(
        "var 'simulated_now' is required for source freshness: it is measured on "
        ~ "simulated time. Run it through platform-ops, which passes the simulated clock."
    ) }}
  {%- endif -%}
  {% call statement('collect_freshness', fetch_result=True, auto_begin=False) -%}
    select
      max({{ loaded_at_field }}) as max_loaded_at,
      cast('{{ simulated_now }}' as timestamp) as snapshotted_at
    from {{ source }}
    {% if filter %}
    where {{ filter }}
    {% endif %}
  {% endcall %}
  {{ return(load_result('collect_freshness')) }}
{% endmacro %}
