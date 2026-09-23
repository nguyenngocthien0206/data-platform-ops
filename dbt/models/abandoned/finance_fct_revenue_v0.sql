-- Revenue before returns were netted out.
-- History: the first revenue model. Replaced by finance_fct_revenue once audit
-- asked for returns to be excluded from recognized revenue. Its name still
-- matches the critical revenue ownership rule, which is why the registry needs
-- a more specific rule to demote it to best effort.
select
    order_id,
    ordered_at,
    channel,
    net_amount as revenue
from {{ ref('int_orders_enriched') }}
where status in ('completed', 'shipped', 'returned')
