{{ config(materialized='table') }}

with usage as (

    select * from {{ ref('stg_usage_events') }}

),

accounts as (

    select * from {{ ref('stg_accounts') }}

),

daily as (

    -- Grain: account x day x product. Summing rather than selecting rows
    -- directly means a duplicated load shows up as inflated revenue here, not
    -- as a broken grain - which is what makes it reconcilable against the
    -- ledger downstream.
    select
        u.account_id,
        u.usage_date,
        u.product,
        sum(u.credits)                  as credits,
        sum(u.revenue)                  as revenue,
        count(*)                        as event_count,
        sum(case when u.is_late then 1 else 0 end) as late_event_count,
        max(u.loaded_at)                as last_loaded_at
    from usage as u
    group by 1, 2, 3

)

select
    {{ surrogate_key(['d.account_id', 'd.usage_date', 'd.product']) }} as daily_revenue_key,
    d.account_id,
    d.usage_date,
    d.product,
    a.segment,
    a.region,
    a.contract_type,
    d.credits,
    d.revenue,
    d.event_count,
    d.late_event_count,
    d.last_loaded_at
from daily as d
inner join accounts as a
    on d.account_id = a.account_id
