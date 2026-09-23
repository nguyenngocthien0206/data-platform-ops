select
    order_id,
    count(*) as item_lines,
    sum(quantity) as units,
    sum(line_amount) as gross_amount,
    sum(line_cost) as cost_amount,
    sum(line_margin) as line_margin_amount
from {{ ref('int_order_items_enriched') }}
group by order_id
