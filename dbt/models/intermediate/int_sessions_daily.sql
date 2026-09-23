select
    session_date,
    device,
    traffic_source,
    count(*) as sessions,
    count(*) filter (where is_known_visitor) as known_visitor_sessions,
    count(*) filter (where pageviews >= 3) as engaged_sessions,
    count(*) filter (where converted) as converted_sessions,
    sum(pageviews) as pageviews,
    sum(duration_seconds) as duration_seconds
from {{ ref('int_sessions_enriched') }}
group by session_date, device, traffic_source
