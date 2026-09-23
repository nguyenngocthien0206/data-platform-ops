-- Customer dimension as the old CRM export defined it, one row per account.
-- History: predates email deduplication, so a person who registered twice
-- appears twice. Superseded by sales_dim_customers; kept around for a CRM sync
-- that was decommissioned in 2025-04.
select
    customer_id,
    first_name,
    last_name,
    email_raw as email,
    country,
    signed_up_at
from {{ ref('stg_customers') }}
