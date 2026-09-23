select
    product_id,
    category,
    cast(ordered_at as date) as sale_date,
    sum(quantity) as units,
    sum(line_amount) as revenue,
    sum(line_margin) as margin
from {{ ref('int_order_items_enriched') }}
where order_status in ('completed', 'shipped')
group by product_id, category, cast(ordered_at as date)
