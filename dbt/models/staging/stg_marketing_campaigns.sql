select
    campaign_id,
    campaign_name,
    channel,
    start_date,
    end_date,
    date_diff('day', start_date, end_date) as planned_days,
    budget,
    _loaded_at
from {{ source('raw', 'marketing_campaigns') }} final
