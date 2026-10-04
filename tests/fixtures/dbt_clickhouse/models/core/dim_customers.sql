select customer_id, email, signed_up_at, country_name, region
from {{ ref('int_customer_countries') }}
