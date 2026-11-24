{{ config(severity='warn') }}
-- Warn: see dq_reconciliation_is_clean.sql.
select * from {{ ref('dq_duplicates') }}
