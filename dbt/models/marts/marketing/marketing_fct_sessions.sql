select
    session_id,
    started_at,
    session_date,
    device,
    landing_page,
    traffic_source,
    campaign_id,
    campaign_channel,
    is_known_visitor,
    pageviews,
    duration_seconds,
    converted
from {{ ref('int_sessions_enriched') }}
