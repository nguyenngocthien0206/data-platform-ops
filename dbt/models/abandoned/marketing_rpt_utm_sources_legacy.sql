-- Traffic by the old UTM taxonomy, which grouped every paid channel as "paid".
-- History: kept for a quarter while the new taxonomy bedded in, "just in case
-- anyone asks for the old numbers". Nobody did. Still builds nightly.
select
    session_date,
    case
        when traffic_source in ('paid_search', 'display', 'social', 'affiliate') then 'paid'
        when traffic_source = 'email' then 'crm'
        else 'other'
    end as legacy_source_group,
    count(*) as sessions
from {{ ref('stg_web_sessions') }}
group by 1, 2
