select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    c.country,
    c.referral_source,
    c.signed_up_at,
    c.duplicate_accounts,
    o.first_order_at,
    o.last_order_at,
    coalesce(o.order_count, 0) as order_count,
    coalesce(s.lifetime_revenue, 0) as lifetime_revenue,
    coalesce(s.value_segment, 'prospect') as value_segment,
    coalesce(s.is_lapsed, false) as is_lapsed
from {{ ref('int_customers_deduplicated') }} c
left join {{ ref('int_customer_orders') }} o on o.canonical_customer_id = c.customer_id
left join {{ ref('int_customer_segments') }} s on s.canonical_customer_id = c.customer_id
