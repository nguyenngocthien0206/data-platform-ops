select
    cast(date_trunc('month', recognized_at) as date) as revenue_month,
    count(*) as orders,
    sum(gross_amount) as gross_revenue,
    sum(discount_amount) as discounts,
    sum(recognized_revenue) as recognized_revenue,
    sum(returned_revenue) as returned_revenue
from {{ ref('finance_fct_revenue') }}
group by 1
