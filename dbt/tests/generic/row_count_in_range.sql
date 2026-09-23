{#
  Row count within an expected band. Bounds are given at scale factor 1.0 and
  scaled by the run's scale_factor var, so the same test holds at every scale.
#}
{% test row_count_in_range(model, min_rows_at_scale_1, max_rows_at_scale_1) %}
{%- set scale = var('scale_factor', 1.0) | float -%}
with counted as (
    select count(*) as row_count from {{ model }}
)
select row_count
from counted
where row_count < {{ (min_rows_at_scale_1 * scale) | round(0, 'floor') | int }}
   or row_count > {{ (max_rows_at_scale_1 * scale) | round(0, 'ceil') | int }}
{% endtest %}
