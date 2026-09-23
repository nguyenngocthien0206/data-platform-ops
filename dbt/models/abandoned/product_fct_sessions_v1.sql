-- First version of the session fact, before customer deduplication existed.
-- History: replaced by int_sessions_enriched in 2025-02. Two notebooks read it
-- for a while; both belong to people who have since moved teams.
select
    session_id,
    customer_id,
    started_at,
    device,
    pageviews,
    duration_seconds,
    converted
from {{ ref('stg_web_sessions') }}
