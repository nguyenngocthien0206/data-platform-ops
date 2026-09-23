-- Signups and first orders per month and referral source: how many people each
-- source brings in, and how many of them go on to buy.
with signups as (
    select
        cast(date_trunc('month', signed_up_at) as date) as month,
        referral_source,
        count(*) as signups
    from {{ ref('int_customers_deduplicated') }}
    group by 1, 2
),

first_orders as (
    select
        cast(date_trunc('month', o.first_order_at) as date) as month,
        c.referral_source,
        count(*) as first_orders
    from {{ ref('int_customer_orders') }} o
    join {{ ref('int_customers_deduplicated') }} c on c.customer_id = o.canonical_customer_id
    group by 1, 2
)

select
    coalesce(s.month, f.month) as month,
    coalesce(s.referral_source, f.referral_source) as referral_source,
    coalesce(s.signups, 0) as signups,
    coalesce(f.first_orders, 0) as first_orders
from signups s
full outer join first_orders f
  on f.month = s.month and f.referral_source = s.referral_source
