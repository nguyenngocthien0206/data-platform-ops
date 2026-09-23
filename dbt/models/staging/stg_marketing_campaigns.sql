select
    campaign_id,
    campaign_name,
    channel,
    start_date,
    end_date,
    end_date - start_date as planned_days,
    budget,
    _loaded_at
from {{ source('raw', 'marketing_campaigns') }}
