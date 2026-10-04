select c.customer_id, c.email, c.signed_up_at, c.country_code, cc.country_name, cc.region
from {{ ref('stg_shop__customers') }} as c
left join {{ ref('country_codes') }} as cc on cc.country_code = c.country_code
