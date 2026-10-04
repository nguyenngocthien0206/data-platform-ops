{% snapshot snap_customers %}
{{ config(target_schema='acme', unique_key='customer_id', strategy='check', check_cols=['email', 'country_code']) }}
select customer_id, email, country_code from {{ source('shop', 'customers') }}
{% endsnapshot %}
