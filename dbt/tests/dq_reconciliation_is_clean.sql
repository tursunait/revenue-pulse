{{ config(severity='warn') }}
-- Warn, not error: this dataset has planted anomalies, so breaks are expected
-- and each one is mapped to an ANOMALY_LABELS row. On real data this would be
-- severity='error'.
select * from {{ ref('dq_reconciliation') }}
