select toMonday(signed_up_at) as week, count() as new_customers
from {{ ref('dim_customers') }}
group by week
