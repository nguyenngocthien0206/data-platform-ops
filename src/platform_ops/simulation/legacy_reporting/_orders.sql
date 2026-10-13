-- Orders with their lines priced and the customer resolved to one account.
-- Every discount code takes 10% off the gross; only orders with lines count.
CREATE VIEW {reporting}._orders AS
WITH lines AS (
    SELECT
        i.order_id,
        count(*) AS item_lines,
        sum(i.quantity) AS units,
        sum(i.quantity * i.unit_price) AS gross_amount,
        sum(i.quantity * p.unit_cost) AS cost_amount
    FROM {app}.order_items i
    JOIN {app}.products p ON p.product_id = i.product_id
    GROUP BY i.order_id
),
priced AS (
    SELECT
        o.order_id,
        o.customer_id,
        m.canonical_customer_id,
        o.ordered_at,
        o.ordered_at::date AS order_date,
        o.status,
        o.channel,
        o.discount_code,
        l.item_lines,
        l.units,
        l.gross_amount,
        l.cost_amount,
        CASE WHEN o.discount_code IS NOT NULL THEN round(l.gross_amount * 0.10, 2) ELSE 0 END
            AS discount_amount
    FROM {app}.orders o
    JOIN lines l ON l.order_id = o.order_id
    LEFT JOIN {reporting}._customer_map m ON m.customer_id = o.customer_id
)
SELECT
    p.*,
    c.country,
    p.gross_amount - p.discount_amount AS net_amount,
    p.gross_amount - p.discount_amount - p.cost_amount AS margin_amount
FROM priced p
LEFT JOIN {app}.customers c ON c.customer_id = p.canonical_customer_id;
