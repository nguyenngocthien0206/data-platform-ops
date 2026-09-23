select
    session_id,
    customer_id,
    started_at,
    cast(started_at as date) as session_date,
    device,
    landing_page,
    coalesce(utm_source, 'direct') as traffic_source,
    campaign_id,
    pageviews,
    duration_seconds,
    converted,
    _loaded_at
from {{ source('raw', 'web_sessions') }}
