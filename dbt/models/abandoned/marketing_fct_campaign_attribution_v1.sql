-- First-touch campaign attribution.
-- History: the original attribution model, built in 2024 for the annual budget
-- review. Replaced by last-touch attribution (int_campaign_attribution) in
-- 2025-06 after marketing and finance agreed on a single definition. The old
-- dashboard was retired, but nobody removed the model, so it still builds nightly.
with first_touch as (
    select
        o.order_id,
        o.net_amount,
        s.campaign_id,
        row_number() over (
            partition by o.order_id order by s.started_at, s.session_id
        ) as touch_rank
    from {{ ref('int_orders_enriched') }} o
    join {{ ref('int_sessions_enriched') }} s
      on s.canonical_customer_id = o.canonical_customer_id
     and s.campaign_id is not null
     and s.started_at <= o.ordered_at
    where o.status in ('completed', 'shipped')
)

select
    campaign_id,
    count(*) as first_touch_orders,
    sum(net_amount) as first_touch_revenue
from first_touch
where touch_rank = 1
group by campaign_id
