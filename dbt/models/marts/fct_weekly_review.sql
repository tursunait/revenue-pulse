{{ config(materialized='table') }}

with daily as (

    select * from {{ ref('fct_daily_revenue') }}

),

weekly as (

    -- Grain: week x segment x product.
    select
        date_trunc('week', usage_date)::date    as week_start,
        segment,
        product,
        sum(revenue)                            as revenue,
        sum(credits)                            as credits,
        count(distinct account_id)              as active_accounts
    from daily
    group by 1, 2, 3

),

compared as (

    select
        week_start,
        segment,
        product,
        revenue,
        credits,
        active_accounts,

        lag(revenue) over (
            partition by segment, product
            order by week_start
        )                                       as prior_week_revenue,

        -- Trailing average of the FOUR WEEKS BEFORE this one. The current week
        -- is excluded on purpose: including it would let a spike pull up its
        -- own baseline and mute the very signal this column exists to show.
        avg(revenue) over (
            partition by segment, product
            order by week_start
            rows between 4 preceding and 1 preceding
        )                                       as trailing_4w_avg_revenue

    from weekly

)

select
    {{ surrogate_key(['week_start', 'segment', 'product']) }} as weekly_review_key,
    week_start,
    segment,
    product,
    revenue,
    credits,
    active_accounts,
    prior_week_revenue,
    trailing_4w_avg_revenue,

    case
        when prior_week_revenue is null or prior_week_revenue = 0 then null
        else round((revenue - prior_week_revenue) / prior_week_revenue * 100, 2)
    end                                         as wow_pct_change,

    case
        when trailing_4w_avg_revenue is null or trailing_4w_avg_revenue = 0 then null
        else round((revenue - trailing_4w_avg_revenue) / trailing_4w_avg_revenue * 100, 2)
    end                                         as vs_trailing_4w_pct
from compared
