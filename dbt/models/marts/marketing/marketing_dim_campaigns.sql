select
    campaign_id,
    campaign_name,
    channel,
    start_date,
    end_date,
    planned_days,
    budget
from {{ ref('stg_marketing_campaigns') }}
