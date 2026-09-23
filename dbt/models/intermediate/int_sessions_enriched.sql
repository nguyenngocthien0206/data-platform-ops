select
    s.session_id,
    s.customer_id,
    m.canonical_customer_id,
    s.started_at,
    s.session_date,
    s.device,
    s.landing_page,
    s.traffic_source,
    s.campaign_id,
    c.campaign_name,
    c.channel as campaign_channel,
    s.pageviews,
    s.duration_seconds,
    s.converted,
    s.customer_id is not null as is_known_visitor
from {{ ref('stg_web_sessions') }} s
left join {{ ref('int_customer_id_map') }} m on m.customer_id = s.customer_id
left join {{ ref('stg_marketing_campaigns') }} c on c.campaign_id = s.campaign_id
