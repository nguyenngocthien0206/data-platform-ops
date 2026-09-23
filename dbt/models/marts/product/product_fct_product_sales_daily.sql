select
    product_id,
    category,
    sale_date,
    units,
    revenue,
    margin
from {{ ref('int_product_sales_daily') }}
