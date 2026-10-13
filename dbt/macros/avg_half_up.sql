{#
  The mean of a column, rounded to `scale` decimals the way the legacy reports
  on Postgres round it: computed exactly, half away from zero.

  ClickHouse's avg() returns a Float64, and round() on a float rounds a half to
  even, so a value of exactly half a cent can land on the other cent. Summing in
  Decimal and dividing by the count keeps the mean exact to six decimals before
  rounding, and round() on a Decimal rounds half away from zero. NULLs are
  skipped, as avg() skips them, and a group with no values gives NULL.
#}
{% macro avg_half_up(expr, scale=2) -%}
toDecimal64(round(toDecimal128(sum({{ expr }}), 6) / nullIf(count({{ expr }}), 0), {{ scale }}), {{ scale }})
{%- endmacro %}
