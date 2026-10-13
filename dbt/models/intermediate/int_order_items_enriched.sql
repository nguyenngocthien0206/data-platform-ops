{{
    config(
        materialized='incremental',
        incremental_strategy='insert_overwrite',
        partition_by='toYYYYMM(ordered_at)',
        order_by='order_item_id',
    )
}}
select
    i.order_item_id as order_item_id,
    i.order_id as order_id,
    o.ordered_at as ordered_at,
    o.status as order_status,
    i.line_number as line_number,
    i.product_id as product_id,
    p.product_name as product_name,
    p.category as category,
    i.quantity as quantity,
    i.unit_price as unit_price,
    i.line_amount as line_amount,
    i.quantity * p.unit_cost as line_cost,
    i.line_amount - i.quantity * p.unit_cost as line_margin
from {{ ref('stg_order_items') }} i
join {{ ref('stg_orders') }} o using (order_id)
join {{ ref('stg_products') }} p using (product_id)
{% if is_incremental() %}
-- Rebuild the current and the previous month in full, replacing those
-- partitions: rows that arrive late land inside the window, and whole
-- partitions are swapped, so every build gives the same answer.
where toStartOfMonth(o.ordered_at) >= (
    select toStartOfMonth(max(ordered_at)) - interval 1 month from {{ this }}
)
{% endif %}
