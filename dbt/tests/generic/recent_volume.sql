{#
  A feed that stops or shrinks, caught the night it happens.

  Rows loaded in the 24 hours before the raw layer's latest load must be at
  least `min_share` of the median of the `days` days before. Freshness alone
  lets a stopped feed through until its error_after passes (two days); this
  fails on the first night with no rows, or with far fewer than usual.

  The clock is the data's own: "now" is the latest `loaded_at` across every
  table of the same schema, which is when the ingestion last ran. So the test
  needs no var, and reads the same in the company's run, under Airflow or
  Dagster, and on a wall clock.

  It calibrates itself: a day is compared with the days before it, at any
  scale. Only for continuous feeds: a source that receives rows now and then
  (a catalogue, campaign setup, tickets in bursts) would only add noise, the
  same reason those sources have no freshness rule. Below `min_usual` rows on
  a usual day the counts are too small to judge (a few customers a day at
  scale 0.01), and the test passes; freshness still covers that feed.
#}
{% test recent_volume(model, loaded_at='_loaded_at', min_share=0.5, days=7, min_usual=10) %}
{%- set siblings = [] -%}
{%- if execute -%}
  {%- for node in graph.sources.values() -%}
    {%- if node.schema == model.schema -%}
      {%- do siblings.append(node.relation_name) -%}
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

daily as (
    -- Day 0 is the 24 hours up to the anchor, day 1 the 24 before, and so on.
    select
        toUInt64(intDiv(dateDiff('second', {{ loaded_at }}, (select at from anchor)), 86400)) as day,
        count() as loaded
    from {{ model }}
    where {{ loaded_at }} > (select at from anchor) - toIntervalDay({{ days + 1 }})
    group by day
),

-- A day nothing was loaded counts as 0, not as missing: dbt-clickhouse sets
-- join_use_nulls, and a median that skips the empty days would compare a
-- normal day with the history's one big seed load.
filled as (
    select n.number as day, ifNull(d.loaded, 0) as loaded
    from numbers({{ days + 1 }}) as n
    left join daily as d on d.day = n.number
)

select today, usual
from (
    select
        sumIf(loaded, day = 0) as today,
        medianExactIf(loaded, day >= 1) as usual
    from filled
)
where usual >= {{ min_usual }} and today < {{ min_share }} * usual
-- ClickHouse's query condition cache remembers which granules a filter let
-- through, and this test and its sibling share the filter on the clock: with it
-- on, what one reads depends on which ran first. The cost report must not.
settings use_query_condition_cache = 0
{% endtest %}
