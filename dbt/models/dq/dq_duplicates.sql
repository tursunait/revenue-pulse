{{ config(materialized='table') }}

{#
    Duplicate detection against RAW, deliberately BEFORE stg_usage_events
    dedupes. Reading the staging model would measure the dedupe, not the data.

    Two different faults are reported, because they need different fixes:

      event_id        the same surrogate key arrived twice - a true replay.
                      stg_usage_events already collapses these.
      business_grain  one account-day-product has more than one row with
                      distinct event_ids - a loader re-run that re-keyed its
                      output. Dedupe on event_id cannot see this, so these
                      rows reach the marts and inflate revenue.
#}

with source as (

    select
        event_id,
        account_id,
        cast(usage_date as date)        as usage_date,
        product,
        credits * price_per_credit      as revenue
    from {{ source('raw', 'USAGE_EVENTS') }}

),

event_id_dupes as (

    select
        'event_id'                      as duplicate_kind,
        account_id,
        usage_date,
        product,
        count(*)                        as row_count,
        count(distinct event_id)        as distinct_event_ids,
        sum(revenue)                    as revenue_at_grain
    from source
    group by 2, 3, 4
    having count(*) > count(distinct event_id)

),

grain_dupes as (

    select
        'business_grain'                as duplicate_kind,
        account_id,
        usage_date,
        product,
        count(*)                        as row_count,
        count(distinct event_id)        as distinct_event_ids,
        sum(revenue)                    as revenue_at_grain
    from source
    group by 2, 3, 4
    having count(*) > 1

)

select * from event_id_dupes
union all
select * from grain_dupes
