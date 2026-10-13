-- Column names are qualified with `d.` inside the aggregates: in ClickHouse a
-- SELECT alias shadows a column of the same name anywhere in the query, so
-- `sum(sessions)` would otherwise read the alias `sessions` defined above it.
{{ config(order_by='(session_date, device)') }}
select
    session_date,
    device,
    sum(d.sessions) as sessions,
    sum(d.engaged_sessions) as engaged_sessions,
    sum(d.converted_sessions) as converted_sessions,
    sum(d.pageviews) as pageviews,
    round(sum(d.duration_seconds) / sum(d.sessions), 2) as avg_duration_seconds
from {{ ref('int_sessions_daily') }} as d
group by session_date, device
