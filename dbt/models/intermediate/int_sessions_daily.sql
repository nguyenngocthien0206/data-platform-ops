{{ config(order_by='(session_date, device, traffic_source)') }}
select
    session_date,
    device,
    traffic_source,
    count(*) as sessions,
    count(*) filter (where is_known_visitor) as known_visitor_sessions,
    count(*) filter (where s.pageviews >= 3) as engaged_sessions,
    count(*) filter (where converted) as converted_sessions,
    sum(s.pageviews) as pageviews,
    sum(s.duration_seconds) as duration_seconds
from {{ ref('int_sessions_enriched') }} as s
group by session_date, device, traffic_source
