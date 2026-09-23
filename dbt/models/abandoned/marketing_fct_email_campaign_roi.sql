-- Return on spend for email campaigns only.
-- History: built for the lifecycle email squad. The squad was merged into
-- marketing analytics in 2025-03 and now uses the all-channel campaign
-- performance mart. This one kept building.
select
    c.campaign_id,
    c.campaign_name,
    sum(s.spend) as spend,
    sum(s.clicks) as clicks,
    round(sum(s.clicks) / nullif(sum(s.impressions), 0), 4) as click_through_rate
from {{ ref('stg_marketing_campaigns') }} c
join {{ ref('stg_marketing_spend') }} s using (campaign_id)
where c.channel = 'email'
group by c.campaign_id, c.campaign_name
