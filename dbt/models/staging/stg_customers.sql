-- Emails arrive with inconsistent case and stray whitespace, which is how the
-- same person ends up with two accounts. Folding happens here, once, so every
-- downstream model agrees on what "the same email" means.
select
    customer_id,
    first_name,
    last_name,
    email as email_raw,
    lower(trim(email)) as email,
    phone,
    country,
    coalesce(referral_source, 'unknown') as referral_source,
    signed_up_at,
    _loaded_at
from {{ source('raw', 'customers') }}
