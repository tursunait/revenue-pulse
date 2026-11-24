with source as (

    select * from {{ source('raw', 'ACCOUNTS') }}

),

renamed as (

    select
        account_id,
        account_name,
        segment,
        region,
        contract_type,
        cast(start_date as date)                as start_date,
        cast(churn_date as date)                as churn_date,
        churn_date is not null                  as is_churned,
        credit_scale
    from source

)

select * from renamed
