select
    spend_id,
    campaign_id,
    spend_date,
    spend,
    impressions,
    clicks,
    _loaded_at
from {{ source('raw', 'marketing_spend') }}
