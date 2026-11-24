{{ config(materialized='table') }}

{#
    Robust z-score per account x product x day, scored against the SAME WEEKDAY
    over the trailing 8 weeks.

    This model scores every account-day; it does not flag. dq_anomalies applies
    the threshold. Splitting them means eval/score_detector.py can sweep
    thresholds without re-running dbt, and the scoring logic exists once.

    Why same-weekday rather than the previous 8 days: consumption has a strong
    weekly cycle (weekends run ~50% of weekdays here), so comparing a Saturday
    against a window containing weekdays reports a fault every weekend.

    Why median/MAD rather than mean/stddev: a mean baseline is dragged by the
    very outlier being looked for, and one 1000x unit error inflates stddev
    enough to hide every other anomaly in the series. MAD * 1.4826 is the
    consistent estimator of sigma for normally distributed data, so the z-score
    keeps its usual interpretation.

    This model MUST NOT read RAW.ANOMALY_LABELS. Only eval/ may.
#}

with observed as (

    select
        account_id,
        usage_date,
        product,
        revenue
    from {{ ref('fct_daily_revenue') }}

),

calendar as (

    select distinct usage_date from observed

),

product_list as (

    select distinct product from observed

),

accounts as (

    select account_id, start_date, churn_date
    from {{ ref('stg_accounts') }}

),

spine as (

    -- A dense grid of the days each account was actually live. Without this,
    -- a pipeline gap is an absent row and therefore invisible: the detector
    -- would have nothing to score. Here it becomes an explicit zero.
    select
        a.account_id,
        c.usage_date,
        p.product
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
        coalesce(o.revenue, 0) as value
    from spine as s
    left join observed as o
        on  s.account_id = o.account_id
        and s.usage_date = o.usage_date
        and s.product    = o.product

),

pairs as (

    -- Each row joined to its same-weekday history. The 7-day stride makes the
    -- weekday match implicit and exact.
    select
        t.account_id,
        t.usage_date,
        t.product,
        t.value,
        h.value as hist_value
    from series as t
    inner join series as h
        on  t.account_id = h.account_id
        and t.product    = h.product
        and datediff('day', h.usage_date, t.usage_date) in (
            {%- for week in range(1, var('trailing_weeks') + 1) %}
            {{ week * 7 }}{% if not loop.last %},{% endif %}
            {%- endfor %}
        )

),

expected as (

    select
        account_id,
        usage_date,
        product,
        value,
        median(hist_value)  as expected_value,
        count(*)            as history_points
    from pairs
    group by 1, 2, 3, 4

),

deviations as (

    select
        p.account_id,
        p.usage_date,
        p.product,
        e.value,
        e.expected_value,
        e.history_points,
        abs(p.hist_value - e.expected_value) as abs_deviation
    from pairs as p
    inner join expected as e
        on  p.account_id = e.account_id
        and p.usage_date = e.usage_date
        and p.product    = e.product

),

dispersion as (

    select
        account_id,
        usage_date,
        product,
        value,
        expected_value,
        history_points,
        median(abs_deviation) as mad
    from deviations
    group by 1, 2, 3, 4, 5, 6

)

select
    account_id,
    usage_date,
    product,
    round(value, 4)                                 as value,
    round(expected_value, 4)                        as expected_value,
    history_points,
    round(mad, 6)                                   as mad,
    round(1.4826 * mad, 6)                          as scaled_mad,

    -- The floor keeps a flat series from producing an infinite z-score.
    round(
        greatest(
            1.4826 * mad,
            {{ var('mad_floor_pct_of_median') }} * expected_value,
            {{ var('mad_floor_abs_usd') }}
        ), 6
    )                                               as denominator,

    case
        when history_points < {{ var('min_history_points') }} then null
        else round(
            (value - expected_value) / greatest(
                1.4826 * mad,
                {{ var('mad_floor_pct_of_median') }} * expected_value,
                {{ var('mad_floor_abs_usd') }}
            ), 4
        )
    end                                             as z,

    (value = 0 and expected_value > 0
        and history_points >= {{ var('min_history_points') }})
                                                    as is_zero_after_nonzero
from dispersion
