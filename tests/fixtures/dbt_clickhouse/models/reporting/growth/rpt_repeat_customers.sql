select customer_id, count() as orders
from {{ ref('fct_orders') }}
group by customer_id
having orders > 1
