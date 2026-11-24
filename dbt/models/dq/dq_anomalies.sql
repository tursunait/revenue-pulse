{{ config(materialized='table') }}

{#
    Flagged anomalies: the exceptions from dq_anomaly_scores.

    Two rules, in precedence order:

      zero_after_nonzero  the account-product billed nothing on a day its
                          same-weekday history says it should have. A gap is an
                          absent row upstream, so this is the rule that catches
                          a pipeline failure rather than a value fault.
      robust_zscore       |z| above var('anomaly_z_threshold').

    Precedence matters for reporting, not detection: a gap day usually trips
    both, and labelling it "zero_after_nonzero" says what actually happened.

    This model MUST NOT read RAW.ANOMALY_LABELS. Only eval/ may.
#}

with scored as (

    -- v2. dq_anomaly_scores (v1) is kept only as the evaluation baseline; see
    -- eval/RESULTS.md for the v1 -> v2 comparison.
    select * from {{ ref('dq_anomaly_scores_v2') }}

)

select
    usage_date,
    account_id,
    product,
    value,
    expected_value                                  as expected,
    z,
    case
        when is_zero_after_nonzero then 'zero_after_nonzero'
        else 'robust_zscore'
    end                                             as rule,
    case when z > 0 then 'up' else 'down' end       as direction,
    sigma,
    history_points,
    spread_points
from scored
where is_zero_after_nonzero
   or abs(z) > {{ var('anomaly_z_threshold') }}
