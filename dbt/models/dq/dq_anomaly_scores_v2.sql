{{ config(materialized='table') }}

{#
    Detector v2. Changes exactly the two defects measured for v1 in
    eval/RESULTS.md, and nothing else.

    v1 defect 1 - growth bias. An 8-week-old same-weekday median systematically
    under-predicts a growing series: v1's mean z was +0.66 with 61% of scores
    positive, worst on the fastest-growing product. v2 fixes this two ways:

      * Work on ln(value + 1). Spikes and unit errors are multiplicative, so in
        log space they become additive shifts of roughly constant size
        regardless of account size - and the multiplicative lognormal noise
        becomes additive and roughly symmetric, which is what a z-score
        assumes. The +1 keeps zeros finite.
      * Shift the baseline by the trend. In log space a multiplicative rescale
        is an addition, so the correction is
            ln(median(last 7 days) + 1) - ln(median(the 7 days 4 weeks ago) + 1)
        Seven-day windows span a whole weekday cycle, so the ratio carries no
        weekday signal. Both windows end before the scored day, so an anomaly
        never corrects its own expectation.

    v1 defect 2 - unstable spread. MAD over 8 same-weekday points had a 5th
    percentile three times below its median, so the denominator collapsed at
    random and ordinary noise scored above 4. v2:

      * Estimates spread from 13 weeks of residuals using EVERY day, not one
        weekday per week - roughly 91 points instead of 8. This is only valid
        because the expectation already conditions on weekday, which leaves
        residuals weekday-neutral.
      * Takes the median of |residual| about zero rather than about the
        residual median. The expectation is itself a median-based prediction,
        so the residual centre is known to be ~0; assuming it saves an entire
        aggregation pass over 20M+ rows.
      * Pools with the product x segment cohort as a LOWER bound: an account is
        not plausibly much quieter than its cohort, so a freak-low estimate is
        lifted rather than trusted.

    Snowflake has no sliding-frame MEDIAN (verified), so each trailing
    statistic comes from a self-join with conditional aggregation.

    This model MUST NOT read RAW.ANOMALY_LABELS. Only eval/ may.
#}

with observed as (

    select account_id, usage_date, product, revenue
    from {{ ref('fct_daily_revenue') }}

),

calendar as (select distinct usage_date from observed),
product_list as (select distinct product from observed),

accounts as (

    select account_id, segment, start_date, churn_date
    from {{ ref('stg_accounts') }}

),

spine as (

    select a.account_id, c.usage_date, p.product, a.segment
    from accounts as a
    cross join calendar as c
    cross join product_list as p
    where c.usage_date >= a.start_date
      and (a.churn_date is null or c.usage_date < a.churn_date)

),

series as (

    select
        s.account_id,
        s.usage_date,
        s.product,
        s.segment,
        coalesce(o.revenue, 0)          as value,
        ln(coalesce(o.revenue, 0) + 1)  as y
    from spine as s
    left join observed as o
        on  s.account_id = o.account_id
        and s.usage_date = o.usage_date
        and s.product    = o.product

),

{%- set baseline_days = var('trailing_weeks') * 7 %}

history as (

    -- One join covering every lookback the expectation needs: the same-weekday
    -- baseline (offsets that are multiples of 7) and both trend windows.
    select
        t.account_id,
        t.usage_date,
        t.product,
        t.segment,
        t.value,
        t.y,
        datediff('day', h.usage_date, t.usage_date) as offset_days,
        h.value                                     as hist_value,
        h.y                                         as hist_y
    from series as t
    inner join series as h
        on  t.account_id = h.account_id
        and t.product    = h.product
        and h.usage_date >= dateadd('day', -{{ baseline_days }}, t.usage_date)
        and h.usage_date <  t.usage_date

),

baseline as (

    select
        account_id,
        usage_date,
        product,
        segment,
        value,
        y,
        median(case when mod(offset_days, 7) = 0 then hist_y end)   as base_y,
        count(case when mod(offset_days, 7) = 0 then 1 end)         as weekday_points,
        median(case when offset_days between 1 and {{ var('trend_window_days') }}
                    then hist_value end)                            as med_recent,
        median(case when offset_days between {{ var('trend_lag_days') + 1 }}
                                         and {{ var('trend_lag_days') + var('trend_window_days') }}
                    then hist_value end)                            as med_lagged
    from history
    group by 1, 2, 3, 4, 5, 6

),

trended as (

    select
        *,
        case
            when med_recent is null or med_lagged is null then 0
            else least(
                greatest(
                    ln(med_recent + 1) - ln(med_lagged + 1),
                    -{{ var('trend_max_log_shift') }}
                ),
                {{ var('trend_max_log_shift') }}
            )
        end                                                         as trend_log_shift
    from baseline

),

residuals as (

    select
        account_id,
        usage_date,
        product,
        segment,
        value,
        y,
        base_y,
        weekday_points,
        trend_log_shift,
        base_y + trend_log_shift                                    as expected_y,
        y - (base_y + trend_log_shift)                              as residual
    from trended
    where base_y is not null
      and weekday_points >= {{ var('min_history_points') }}

),

{%- set spread_days = var('spread_weeks') * 7 %}

spread_history as (

    select
        t.account_id,
        t.usage_date,
        t.product,
        abs(h.residual) as abs_residual
    from residuals as t
    inner join residuals as h
        on  t.account_id = h.account_id
        and t.product    = h.product
        and h.usage_date >= dateadd('day', -{{ spread_days }}, t.usage_date)
        and h.usage_date <  t.usage_date

),

account_spread as (

    select
        account_id,
        usage_date,
        product,
        1.4826 * median(abs_residual)   as account_sigma,
        count(*)                        as spread_points
    from spread_history
    group by 1, 2, 3

),

pooled_spread as (

    -- Cohort dispersion for product x segment. A nuisance parameter estimated
    -- from tens of thousands of rows, so a handful of anomalies cannot move
    -- its median.
    select
        segment,
        product,
        1.4826 * median(abs(residual))  as pooled_sigma
    from residuals
    group by 1, 2

)

select
    r.account_id,
    r.usage_date,
    r.product,
    r.segment,
    round(r.value, 4)                                       as value,
    round(exp(r.expected_y) - 1, 4)                         as expected_value,
    round(r.y, 6)                                           as y,
    round(r.expected_y, 6)                                  as expected_y,
    round(r.trend_log_shift, 6)                             as trend_log_shift,
    round(r.residual, 6)                                    as residual,
    r.weekday_points                                        as history_points,
    coalesce(s.spread_points, 0)                            as spread_points,
    round(s.account_sigma, 6)                               as account_sigma,
    round(p.pooled_sigma, 6)                                as pooled_sigma,

    round(
        greatest(
            coalesce(s.account_sigma, 0),
            {{ var('spread_pool_weight') }} * coalesce(p.pooled_sigma, 0),
            ln(1 + {{ var('spread_floor_pct') }})
        ), 6
    )                                                       as sigma,

    case
        when coalesce(s.spread_points, 0) < {{ var('min_spread_points') }} then null
        else round(
            r.residual / greatest(
                coalesce(s.account_sigma, 0),
                {{ var('spread_pool_weight') }} * coalesce(p.pooled_sigma, 0),
                ln(1 + {{ var('spread_floor_pct') }})
            ), 4
        )
    end                                                     as z,

    (r.value = 0 and exp(r.expected_y) - 1 > 0
        and coalesce(s.spread_points, 0) >= {{ var('min_spread_points') }})
                                                            as is_zero_after_nonzero
from residuals as r
left join account_spread as s
    on  r.account_id = s.account_id
    and r.usage_date = s.usage_date
    and r.product    = s.product
left join pooled_spread as p
    on  r.segment  = p.segment
    and r.product  = p.product
