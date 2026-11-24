-- Revenue can be zero but never negative. A negative value would mean either
-- negative credits or a negative price, both of which are bugs rather than
-- anomalies - nothing in the generator plants them.
select
    account_id,
    usage_date,
    product,
    credits,
    revenue
from {{ ref('fct_daily_revenue') }}
where revenue < 0
   or credits < 0
