-- One-off backfill of Q1 2025 orders, built during the warehouse migration.
-- History: meant to run once and be deleted after the cutover was signed off.
-- The cutover was signed off; the model was not deleted.
select *
from {{ ref('int_orders_enriched') }}
where ordered_at >= timestamp '2025-01-01'
  and ordered_at < timestamp '2025-04-01'
