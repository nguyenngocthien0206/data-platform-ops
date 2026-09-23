-- Lifetime performance per campaign. Return on ad spend uses last-touch
-- attributed revenue, which is the definition marketing and finance agreed on.
select
    c.campaign_id,
    c.campaign_name,
    c.channel,
    c.start_date,
    c.end_date,
    c.budget,
    coalesce(sum(p.spend), 0) as spend,
    coalesce(sum(p.impressions), 0) as impressions,
    coalesce(sum(p.clicks), 0) as clicks,
    coalesce(sum(p.sessions), 0) as sessions,
    coalesce(sum(p.attributed_orders), 0) as attributed_orders,
    coalesce(sum(p.attributed_revenue), 0) as attributed_revenue,
    round(sum(p.attributed_revenue) / nullif(sum(p.spend), 0), 4) as return_on_ad_spend
from {{ ref('marketing_dim_campaigns') }} c
left join {{ ref('int_campaign_performance_daily') }} p using (campaign_id)
group by c.campaign_id, c.campaign_name, c.channel, c.start_date, c.end_date, c.budget
