{{
    config(
        materialized='incremental',
        incremental_strategy='insert_overwrite',
        partition_by='toYYYYMM(started_at)',
        order_by='session_id',
    )
}}
select
    s.session_id as session_id,
    s.customer_id as customer_id,
    m.canonical_customer_id as canonical_customer_id,
    s.started_at as started_at,
    s.session_date as session_date,
    s.device as device,
    s.landing_page as landing_page,
    s.traffic_source as traffic_source,
    s.campaign_id as campaign_id,
    c.campaign_name as campaign_name,
    c.channel as campaign_channel,
    s.pageviews as pageviews,
    s.duration_seconds as duration_seconds,
    s.converted as converted,
    s.customer_id is not null as is_known_visitor
from {{ ref('stg_web_sessions') }} s
left join {{ ref('int_customer_id_map') }} m on m.customer_id = s.customer_id
left join {{ ref('stg_marketing_campaigns') }} c on c.campaign_id = s.campaign_id
{% if is_incremental() %}
-- Rebuild the current and the previous month in full, replacing those
-- partitions: rows that arrive late land inside the window, and whole
-- partitions are swapped, so every build gives the same answer.
where toStartOfMonth(s.started_at) >= (
    select toStartOfMonth(max(started_at)) - interval 1 month from {{ this }}
)
{% endif %}
