{#
  Customer history, one row per version. Customers change in place in the
  legacy system (a new phone number, a referral source filled in later), so
  without a snapshot the warehouse only ever knows the latest version.

  The timestamp strategy on updated_at, not the check strategy: dbt stamps a
  check snapshot with the wall clock, while updated_at is the legacy system's
  own (simulated) time, so two runs record the same history.
#}
{% snapshot snap_customers %}
{{
    config(
        target_schema=var('namespace', '') ~ 'snapshots',
        unique_key='customer_id',
        strategy='timestamp',
        updated_at='updated_at',
    )
}}
select customer_id, email, phone, country, referral_source, updated_at
from {{ source('raw', 'customers') }} final
{% endsnapshot %}
