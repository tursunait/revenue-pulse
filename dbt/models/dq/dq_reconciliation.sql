{{ config(materialized='table') }}

{#
    Usage revenue vs finance's booked revenue, per account-day.

    This is an EXCEPTIONS table: it holds only the account-days that break both
    thresholds, so "zero rows" is the healthy state.

    Both gates must be cleared (DECISIONS.md part 2). booked_revenue is rounded
    to cents, so a clean account-day differs from usage revenue by up to half a
    cent - which on a $1.10 account-day is 0.43%. A percentage-only rule fires
    on the smallest accounts every day; a dollar-only rule misses a large
    percentage break on a big account.
#}

with usage_daily as (

    select
        account_id,
        usage_date                      as activity_date,
        sum(revenue)                    as usage_revenue
    from {{ ref('stg_usage_events') }}
    group by 1, 2

),

ledger_daily as (

    select
        account_id,
        revenue_date                    as activity_date,
        sum(booked_revenue)             as ledger_revenue
    from {{ ref('stg_ledger_revenue') }}
    group by 1, 2

),

joined as (

    -- FULL OUTER so a pipeline gap (usage missing, ledger present) is a break
    -- rather than a silently dropped row.
    select
        coalesce(u.account_id, l.account_id)        as account_id,
        coalesce(u.activity_date, l.activity_date)  as activity_date,
        coalesce(u.usage_revenue, 0)                as usage_revenue,
        coalesce(l.ledger_revenue, 0)               as ledger_revenue
    from usage_daily as u
    full outer join ledger_daily as l
        on  u.account_id    = l.account_id
        and u.activity_date = l.activity_date

),

measured as (

    select
        account_id,
        activity_date,
        usage_revenue,
        ledger_revenue,
        round(usage_revenue - ledger_revenue, 4)    as abs_diff_usd,
        case
            when ledger_revenue = 0 then null
            else round((usage_revenue - ledger_revenue) / ledger_revenue * 100, 4)
        end                                         as rel_diff_pct
    from joined

)

select
    account_id,
    activity_date,
    usage_revenue,
    ledger_revenue,
    abs_diff_usd,
    rel_diff_pct,
    case
        when usage_revenue > ledger_revenue then 'usage_above_ledger'
        else 'usage_below_ledger'
    end                                             as direction,
    case
        when ledger_revenue = 0 and usage_revenue > 0 then 'ledger_missing'
        when usage_revenue = 0 and ledger_revenue > 0 then 'usage_missing'
        else 'amount_mismatch'
    end                                             as break_kind
from measured
where abs(abs_diff_usd) > {{ var('recon_abs_usd') }}
  and (
        rel_diff_pct is null                        -- ledger has nothing to compare against
        or abs(rel_diff_pct) > {{ var('recon_rel_pct') }}
      )
