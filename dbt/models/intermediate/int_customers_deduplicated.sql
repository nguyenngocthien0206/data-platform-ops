with duplicates as (
    select canonical_customer_id, count(*) - 1 as duplicate_accounts
    from {{ ref('int_customer_id_map') }}
    group by canonical_customer_id
)

select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    c.phone,
    c.country,
    c.referral_source,
    c.signed_up_at,
    d.duplicate_accounts
from {{ ref('stg_customers') }} c
join duplicates d on d.canonical_customer_id = c.customer_id
