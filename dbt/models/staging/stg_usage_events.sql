with source as (

    select * from {{ source('raw', 'USAGE_EVENTS') }}

),

typed as (

    select
        event_id,
        account_id,
        cast(usage_date as date)                        as usage_date,
        product,
        credits,
        price_per_credit,
        credits * price_per_credit                      as revenue,
        loaded_at,
        -- Hours, not days: usage_date is midnight and normal loads land ~26h
        -- later, so a day-granularity comparison would sit right on the
        -- boundary. See var late_after_hours in dbt_project.yml.
        datediff('hour', cast(usage_date as date), loaded_at)
            > {{ var('late_after_hours') }}              as is_late
    from source

),

deduped as (

    -- Guards against a loader replay that re-sends the same event_id, keeping
    -- the first time we saw each event.
    --
    -- Note this does NOT collapse the planted duplicate_load anomalies: a
    -- loader re-run issues fresh surrogate keys, so those rows are distinct
    -- events at this grain and survive into the marts on purpose. They are
    -- caught by dq_duplicates and by dq_reconciliation against the ledger.
    select *
    from typed
    qualify row_number() over (
        partition by event_id
        order by loaded_at asc
    ) = 1

)

select * from deduped
