-- Catalog performance: units and revenue per month and category, sold orders only.
CREATE VIEW {reporting}.product_sales_monthly_by_category AS
SELECT
    date_trunc('month', o.ordered_at)::date AS period_start,
    p.category,
    sum(i.quantity) AS units,
    sum(i.quantity * i.unit_price) AS revenue
FROM {app}.order_items i
JOIN {app}.orders o ON o.order_id = i.order_id
JOIN {app}.products p ON p.product_id = i.product_id
WHERE o.status IN ('completed', 'shipped')
GROUP BY 1, 2;
