select
    a.order_id,
    a.ordered_at,
    a.net_amount,
    a.campaign_id,
    c.campaign_name,
    a.attributed_channel,
    a.touch_at,
    date_diff('hour', a.touch_at, a.ordered_at) as hours_from_touch
from {{ ref('int_campaign_attribution') }} a
join {{ ref('marketing_dim_campaigns') }} c using (campaign_id)
