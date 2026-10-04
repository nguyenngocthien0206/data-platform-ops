select toStartOfMonth(calendar_date) as month, sum(revenue) as revenue
from {{ ref('rpt_revenue_daily') }}
group by month
