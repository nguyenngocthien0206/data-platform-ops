select
    activity_date,
    channel,
    sum(spend) as spend,
    sum(impressions) as impressions,
    sum(clicks) as clicks,
    sum(sessions) as sessions,
    sum(attributed_orders) as attributed_orders,
    sum(attributed_revenue) as attributed_revenue
from {{ ref('int_campaign_performance_daily') }}
group by activity_date, channel
