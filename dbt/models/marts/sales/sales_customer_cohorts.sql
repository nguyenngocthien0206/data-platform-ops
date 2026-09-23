-- Monthly signup cohorts and how many of each cohort ordered in each later month.
with cohorts as (
    select customer_id, cast(date_trunc('month', signed_up_at) as date) as cohort_month
    from {{ ref('sales_dim_customers') }}
),

activity as (
    select distinct customer_id, cast(date_trunc('month', ordered_at) as date) as order_month
    from {{ ref('sales_fct_orders') }}
    where status <> 'cancelled'
)

select
    c.cohort_month,
    date_diff('month', c.cohort_month, a.order_month) as months_since_signup,
    count(*) as active_customers
from cohorts c
join activity a using (customer_id)
where a.order_month >= c.cohort_month
group by 1, 2
