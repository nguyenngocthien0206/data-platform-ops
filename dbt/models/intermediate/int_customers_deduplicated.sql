{{ config(order_by='customer_id') }}
with duplicates as (
    select canonical_customer_id, count(*) - 1 as duplicate_accounts
    from {{ ref('int_customer_id_map') }}
    group by canonical_customer_id
)

select
    c.customer_id as customer_id,
    c.first_name as first_name,
    c.last_name as last_name,
    c.email as email,
    c.phone as phone,
    c.country as country,
    c.referral_source as referral_source,
    c.signed_up_at as signed_up_at,
    d.duplicate_accounts as duplicate_accounts
from {{ ref('stg_customers') }} c
join duplicates d on d.canonical_customer_id = c.customer_id
