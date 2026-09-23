select
    session_date,
    device,
    sum(sessions) as sessions,
    sum(engaged_sessions) as engaged_sessions,
    sum(converted_sessions) as converted_sessions,
    sum(pageviews) as pageviews,
    round(sum(duration_seconds) / sum(sessions), 2) as avg_duration_seconds
from {{ ref('int_sessions_daily') }}
group by session_date, device
