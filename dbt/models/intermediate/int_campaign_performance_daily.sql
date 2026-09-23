with spend as (
    select
        campaign_id,
        spend_date as activity_date,
        sum(spend) as spend,
        sum(impressions) as impressions,
        sum(clicks) as clicks
    from {{ ref('stg_marketing_spend') }}
    group by 1, 2
),

sessions as (
    select campaign_id, session_date as activity_date, count(*) as sessions
    from {{ ref('int_sessions_enriched') }}
    where campaign_id is not null
    group by 1, 2
),

orders as (
    select
        campaign_id,
        cast(ordered_at as date) as activity_date,
        count(*) as attributed_orders,
        sum(net_amount) as attributed_revenue
    from {{ ref('int_campaign_attribution') }}
    group by 1, 2
),

keys as (
    select campaign_id, activity_date from spend
    union
    select campaign_id, activity_date from sessions
    union
    select campaign_id, activity_date from orders
)

select
    k.campaign_id,
    c.campaign_name,
    c.channel,
    k.activity_date,
    coalesce(sp.spend, 0) as spend,
    coalesce(sp.impressions, 0) as impressions,
    coalesce(sp.clicks, 0) as clicks,
    coalesce(se.sessions, 0) as sessions,
    coalesce(o.attributed_orders, 0) as attributed_orders,
    coalesce(o.attributed_revenue, 0) as attributed_revenue
from keys k
join {{ ref('stg_marketing_campaigns') }} c using (campaign_id)
left join spend sp using (campaign_id, activity_date)
left join sessions se using (campaign_id, activity_date)
left join orders o using (campaign_id, activity_date)
