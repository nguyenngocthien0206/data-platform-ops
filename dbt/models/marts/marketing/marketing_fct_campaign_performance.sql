-- Lifetime performance per campaign. Return on ad spend uses last-touch
-- attributed revenue, which is the definition marketing and finance agreed on.
{{ config(order_by='campaign_id') }}
select
    c.campaign_id as campaign_id,
    c.campaign_name as campaign_name,
    c.channel as channel,
    c.start_date as start_date,
    c.end_date as end_date,
    c.budget as budget,
    coalesce(sum(p.spend), 0) as spend,
    coalesce(sum(p.impressions), 0) as impressions,
    coalesce(sum(p.clicks), 0) as clicks,
    coalesce(sum(p.sessions), 0) as sessions,
    coalesce(sum(p.attributed_orders), 0) as attributed_orders,
    coalesce(sum(p.attributed_revenue), 0) as attributed_revenue,
    -- As the legacy report has it (the Phase 12 parallel run compares them): no
    -- attributed order means no return on ad spend (NULL, not 0), and the ratio
    -- is rounded, not truncated. ClickHouse divides two Decimals at the
    -- dividend's scale, so the division runs at 10 decimals first.
    if(
        sum(p.attributed_orders) = 0,
        null,
        toDecimal128(
            round(toDecimal128(sum(p.attributed_revenue), 10) / nullif(sum(p.spend), 0), 4), 4
        )
    ) as return_on_ad_spend
from {{ ref('marketing_dim_campaigns') }} c
left join {{ ref('int_campaign_performance_daily') }} p using (campaign_id)
group by c.campaign_id, c.campaign_name, c.channel, c.start_date, c.end_date, c.budget
