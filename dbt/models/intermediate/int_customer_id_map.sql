-- Every account id mapped to the canonical account for that person: the
-- earliest signup sharing the folded email. Facts join through this map so a
-- customer who registered twice is counted once.
select
    customer_id,
    first_value(customer_id) over (
        partition by email order by signed_up_at, customer_id
    ) as canonical_customer_id
from {{ ref('stg_customers') }}
