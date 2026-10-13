-- One canonical account per person: duplicate sign-ups share an email, and
-- the earliest account (then the lowest id) stands for all of them.
CREATE VIEW {reporting}._customer_map AS
SELECT
    customer_id,
    first_value(customer_id) OVER (
        PARTITION BY lower(trim(email)) ORDER BY signed_up_at, customer_id
    ) AS canonical_customer_id
FROM {app}.customers;
