select cal.calendar_date, sum(o.amount) as revenue
from {{ ref('shared_calendar') }} as cal
left join (
    select order_date, amount from {{ ref('fct_orders') }} where status = 'completed'
) as o on o.order_date = cal.calendar_date
group by cal.calendar_date
