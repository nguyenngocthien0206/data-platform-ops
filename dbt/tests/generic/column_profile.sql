{#
  Wrong values that no test checks, caught the night they arrive.

  For every column of a continuous feed, the rows loaded in the 24 hours before
  the raw layer's latest load are compared with the `days` days before:

  - the share of NULLs must not rise more than `null_margin` above the usual
    (the median of those days) by more than chance explains: `min_z` standard
    errors of a day of that size. A loader that starts writing NULLs into a
    column nobody tests fails; a column that is often empty anyway (referral
    source, a third of the time) does not fail on a quiet day of 40 rows;
  - for a text column with at most `max_distinct` values over those days, at
    most `new_value_share` of today's rows may carry a value never seen in them:
    a category the upstream system started sending.

  The columns come from the warehouse (`adapter.get_columns_in_relation`), so a
  column added later is profiled without a change here. The clock is the
  data's own, as in `recent_volume`. Below `min_rows` rows today there is too
  little to judge, and the test passes. A failing row names the column and the
  measure.
#}
{% test column_profile(model, loaded_at='_loaded_at', days=7, null_margin=0.2, min_z=4, new_value_share=0.1, max_distinct=50, min_rows=10) %}
{%- set siblings = [] -%}
{%- set columns = [] -%}
{%- if execute -%}
  {%- for node in graph.sources.values() -%}
    {%- if node.schema == model.schema -%}
      {%- do siblings.append(node.relation_name) -%}
    {%- endif -%}
  {%- endfor -%}
  {%- for column in adapter.get_columns_in_relation(model) -%}
    {%- if column.name not in (loaded_at, 'updated_at') -%}
      {%- do columns.append(column) -%}
    {%- endif -%}
  {%- endfor -%}
{%- endif -%}
with anchor as (
    select max(loaded) as at
    from (
        {%- for relation in siblings %}
        select max({{ loaded_at }}) as loaded from {{ relation }}
        {%- if not loop.last %} union all{% endif %}
        {%- endfor %}
    )
),

recent as (
    -- Day 0 is the 24 hours up to the anchor, day 1 the 24 before, and so on.
    select
        *,
        toUInt64(intDiv(dateDiff('second', {{ loaded_at }}, (select at from anchor)), 86400)) as _day
    from {{ model }}
    where {{ loaded_at }} > (select at from anchor) - toIntervalDay({{ days + 1 }})
        and {{ loaded_at }} <= (select at from anchor)
)

{% for column in columns -%}
{%- set c = adapter.quote(column.name) -%}
{%- if not loop.first %}
union all
{% endif -%}
select '{{ column.name }}' as column_name, 'null share' as measure, today, usual
from (
    select
        sumIf(share, _day = 0) as today,
        medianExactIf(share, _day >= 1) as usual,
        sumIf(loaded, _day = 0) as today_rows
    from (
        select _day, count() as loaded, countIf(isNull({{ c }})) / count() as share
        from recent
        group by _day
    )
)
where today_rows >= {{ min_rows }} and today > usual + {{ null_margin }}
    and (today - usual) * sqrt(today_rows) > {{ min_z }} * sqrt(greatest(usual * (1 - usual), 1e-9))
{%- if 'String' in column.data_type %}
union all
select '{{ column.name }}' as column_name, 'new values' as measure, today, toFloat64(0) as usual
from (
    -- One pass, no IN (subquery): ClickHouse builds the set of an IN as it
    -- goes, so how much such a query reads changes from run to run, and the
    -- cost report must not. A value is new when its oldest day is today.
    select
        sumIf(on_day_0, oldest = 0) / sum(on_day_0) as today,
        countIf(oldest >= 1) as seen,
        sum(on_day_0) as today_rows
    from (
        select {{ c }} as value, max(_day) as oldest, countIf(_day = 0) as on_day_0
        from recent
        where {{ c }} is not null
        group by value
    )
)
where today_rows >= {{ min_rows }} and seen > 0 and seen <= {{ max_distinct }}
    and today > {{ new_value_share }}
{%- endif %}
{% endfor %}
-- ClickHouse's query condition cache remembers which granules a filter let
-- through, and this test and its sibling share the filter on the clock: with it
-- on, what one reads depends on which ran first. The cost report must not.
settings use_query_condition_cache = 0
{% endtest %}
