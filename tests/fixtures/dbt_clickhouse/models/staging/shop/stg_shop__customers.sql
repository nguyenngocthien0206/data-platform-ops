select customer_id, lower(email) as email, country_code, signed_up_at
from {{ source('shop', 'customers') }}
