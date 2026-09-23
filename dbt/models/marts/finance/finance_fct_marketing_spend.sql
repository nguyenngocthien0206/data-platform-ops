-- Marketing spend as finance books it: a monthly cost per channel.
select
    cast(date_trunc('month', activity_date) as date) as spend_month,
    channel,
    sum(spend) as spend,
    sum(attributed_revenue) as attributed_revenue
from {{ ref('int_campaign_performance_daily') }}
group by 1, 2
