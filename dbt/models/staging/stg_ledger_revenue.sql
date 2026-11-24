with source as (

    select * from {{ source('raw', 'LEDGER_REVENUE') }}

),

renamed as (

    select
        ledger_id,
        account_id,
        cast(revenue_date as date)              as revenue_date,
        booked_revenue,
        currency,
        source_system,
        loaded_at
    from source

)

select * from renamed
