-- fct_weekly_review must be a faithful rollup of fct_daily_revenue: every
-- dollar in the daily fact appears exactly once in the weekly one.
--
-- A tolerance of one cent per segment-product-week absorbs float addition
-- ordering, which differs between the two aggregations. Anything larger means
-- the rollup is dropping or duplicating rows - for instance if the weekly
-- grain lost a segment, or a join fanned out.
with daily_by_week as (

    select
        date_trunc('week', usage_date)::date    as week_start,
        segment,
        product,
        sum(revenue)                            as daily_revenue
    from {{ ref('fct_daily_revenue') }}
    group by 1, 2, 3

),

weekly as (

    select
        week_start,
        segment,
        product,
        revenue                                 as weekly_revenue
    from {{ ref('fct_weekly_review') }}

)

select
    coalesce(d.week_start, w.week_start)        as week_start,
    coalesce(d.segment, w.segment)              as segment,
    coalesce(d.product, w.product)              as product,
    d.daily_revenue,
    w.weekly_revenue,
    abs(coalesce(d.daily_revenue, 0) - coalesce(w.weekly_revenue, 0)) as diff
from daily_by_week as d
full outer join weekly as w
    on  d.week_start = w.week_start
    and d.segment    = w.segment
    and d.product    = w.product
where abs(coalesce(d.daily_revenue, 0) - coalesce(w.weekly_revenue, 0)) > 0.01
