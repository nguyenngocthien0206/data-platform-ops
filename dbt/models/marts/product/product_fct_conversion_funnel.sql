-- Visit, engage, convert: the funnel the product team watches daily per device.
select
    session_date as activity_date,
    device,
    count(*) as sessions,
    count(*) filter (where is_known_visitor) as known_visitor_sessions,
    count(*) filter (where pageviews >= 3) as engaged_sessions,
    count(*) filter (where converted) as converted_sessions,
    round(count(*) filter (where converted) / count(*), 4) as conversion_rate
from {{ ref('int_sessions_enriched') }}
group by session_date, device
