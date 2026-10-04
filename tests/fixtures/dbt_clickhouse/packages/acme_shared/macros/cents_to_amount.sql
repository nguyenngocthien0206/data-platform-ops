{% macro cents_to_amount(column) -%}
    toDecimal64({{ column }} / 100, 2)
{%- endmacro %}
