{{
    config(
        materialized='incremental',
        incremental_strategy='insert_overwrite',
        partition_by='toYYYYMM(started_at)',
        order_by='session_id',
    )
}}
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
{% if is_incremental() %}
-- Rebuild the current and the previous month in full, replacing those
-- partitions: rows that arrive late land inside the window, and whole
-- partitions are swapped, so every build gives the same answer.
where toStartOfMonth(started_at) >= (
    select toStartOfMonth(max(started_at)) - interval 1 month from {{ this }}
)
{% endif %}
