{{ config(materialized='table') }}

{#
    Account-days whose rows landed more than var('late_after_hours') after the
    usage date. The values are correct - this is a freshness fault, not an
    amount fault, so it will never show up in dq_reconciliation.
#}

select
    account_id,
    usage_date,
    count(*)                                            as late_row_count,
    min(loaded_at)                                      as first_loaded_at,
    max(loaded_at)                                      as last_loaded_at,
    round(
        datediff('hour', usage_date, min(loaded_at)) / 24.0, 2
    )                                                   as lag_days,
    sum(revenue)                                        as revenue_affected
from {{ ref('stg_usage_events') }}
where is_late
group by 1, 2
