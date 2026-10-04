select c.country_name, count() as orders, sum(o.amount) as amount
from {{ ref('fct_orders') }} as o
join {{ ref('dim_customers') }} as c on c.customer_id = o.customer_id
group by c.country_name
