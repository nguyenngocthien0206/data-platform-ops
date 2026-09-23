-- Feature table for a churn prediction proof of concept.
-- History: the model was never deployed. The feature table was supposed to be
-- torn down with the POC environment and survived because it lives in dbt.
select
    s.canonical_customer_id as customer_id,
    s.lifetime_revenue,
    s.order_count,
    s.is_lapsed,
    count(t.ticket_id) as support_tickets
from {{ ref('int_customer_segments') }} s
left join {{ ref('int_tickets_enriched') }} t
  on t.canonical_customer_id = s.canonical_customer_id
group by 1, 2, 3, 4
