{{
    config(
        materialized='incremental',
        incremental_strategy='insert_overwrite',
        partition_by='toYYYYMM(ordered_at)',
        order_by='order_item_id',
    )
}}
select
    order_item_id,
    order_id,
    ordered_at,
    order_status,
    product_id,
    product_name,
    category,
    quantity,
    unit_price,
    line_amount,
    line_margin
from {{ ref('int_order_items_enriched') }}
{% if is_incremental() %}
-- Rebuild the current and the previous month in full, replacing those
-- partitions: rows that arrive late land inside the window, and whole
-- partitions are swapped, so every build gives the same answer.
where toStartOfMonth(ordered_at) >= (
    select toStartOfMonth(max(ordered_at)) - interval 1 month from {{ this }}
)
{% endif %}
