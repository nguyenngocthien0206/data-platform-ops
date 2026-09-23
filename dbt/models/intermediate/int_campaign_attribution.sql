-- Last-touch attribution: an order is credited to the most recent campaign
-- session by the same person in the seven days before the order. This replaced
-- the first-touch model in 2025-06 (abandoned/marketing_fct_campaign_attribution_v1).
with touches as (
    select
        o.order_id,
        o.ordered_at,
        o.net_amount,
        s.session_id,
        s.campaign_id,
        s.campaign_channel,
        s.started_at as touch_at,
        row_number() over (
            partition by o.order_id order by s.started_at desc, s.session_id desc
        ) as touch_rank
    from {{ ref('int_orders_enriched') }} o
    join {{ ref('int_sessions_enriched') }} s
      on s.canonical_customer_id = o.canonical_customer_id
     and s.campaign_id is not null
     and s.started_at <= o.ordered_at
     and s.started_at > o.ordered_at - interval 7 day
    where o.status in ('completed', 'shipped')
)

select
    order_id,
    ordered_at,
    net_amount,
    session_id as touch_session_id,
    campaign_id,
    campaign_channel as attributed_channel,
    touch_at
from touches
where touch_rank = 1
