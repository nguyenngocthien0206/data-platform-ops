with sales as (
    select
        product_id,
        min(sale_date) as first_sold_date,
        max(sale_date) as last_sold_date,
        sum(units) as lifetime_units,
        sum(revenue) as lifetime_revenue
    from {{ ref('int_product_sales_daily') }}
    group by product_id
)

select
    p.product_id,
    p.product_name,
    p.category,
    p.unit_price,
    p.unit_cost,
    p.is_active,
    s.first_sold_date,
    s.last_sold_date,
    coalesce(s.lifetime_units, 0) as lifetime_units,
    coalesce(s.lifetime_revenue, 0) as lifetime_revenue
from {{ ref('stg_products') }} p
left join sales s using (product_id)
