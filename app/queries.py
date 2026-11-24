"""Every query the app runs, in one module, each cached with st.cache_data.

Runs inside Streamlit in Snowflake: the session comes from
get_active_session(), so there are no credentials here and nothing is read from
.env. The app inherits the privileges of the role that owns it.

What this module may read:
  MARTS.FCT_DAILY_REVENUE, MARTS.FCT_WEEKLY_REVIEW
  DQ.DQ_RECONCILIATION, DQ.DQ_DUPLICATES, DQ.DQ_LATE_ARRIVALS
  DQ.DQ_ANOMALIES, DQ.DQ_ANOMALY_SCORES_V2
  DQ.DETECTOR_SCORECARD
  STAGING.STG_ACCOUNTS

What it must never read: RAW.ANOMALY_LABELS. The scorecard exists so detector
quality can be displayed without it - eval/score_detector.py is the only thing
that reads labels, and it writes DQ.DETECTOR_SCORECARD for this app.

Cache TTL is 15 minutes. The warehouse auto-suspends after 60s, so every cache
miss pays a cold-start; a short TTL keeps the app responsive without serving
stale numbers through a working session.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

DB = "REVENUE_PULSE"
TTL = 900

#: |z| above which an item enters the review queue. Matches the recommendation
#: in eval/RESULTS.md: page on |z|>5 only when no ledger exists, review from 3.
REVIEW_Z = 3.0

#: Weeks of history detector v2 needs before it will score an account-product.
MIN_HISTORY_WEEKS = 13


def session():
    return get_active_session()


def _df(sql: str, params: list | None = None) -> pd.DataFrame:
    result = session().sql(sql, params=params) if params else session().sql(sql)
    return result.to_pandas()


# --------------------------------------------------------------------------
# Weekly review
# --------------------------------------------------------------------------

@st.cache_data(ttl=TTL)
def weeks() -> list:
    """Available week_start values, newest first."""
    df = _df(f"SELECT DISTINCT WEEK_START FROM {DB}.MARTS.FCT_WEEKLY_REVIEW ORDER BY 1 DESC")
    return df["WEEK_START"].tolist()


@st.cache_data(ttl=TTL)
def week_kpis(week_start) -> dict:
    """Revenue, WoW %, vs trailing 4-week average, active accounts for one week.

    Aggregated up from the segment x product grain, so the WoW and trailing
    comparisons are recomputed at week level rather than averaged from the
    per-row percentages - averaging ratios would weight a tiny segment the same
    as the largest one.
    """
    df = _df(
        f"""
        WITH weekly AS (
            SELECT WEEK_START,
                   SUM(REVENUE)                AS revenue,
                   SUM(ACTIVE_ACCOUNTS)        AS active_rows
            FROM {DB}.MARTS.FCT_WEEKLY_REVIEW
            GROUP BY 1
        ),
        accounts AS (
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
                   COUNT(DISTINCT ACCOUNT_ID)           AS active_accounts
            FROM {DB}.MARTS.FCT_DAILY_REVENUE
            GROUP BY 1
        ),
        ranked AS (
            SELECT w.WEEK_START, w.revenue, a.active_accounts,
                   LAG(w.revenue) OVER (ORDER BY w.WEEK_START) AS prior_revenue,
                   AVG(w.revenue) OVER (ORDER BY w.WEEK_START
                                        ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING)
                                                               AS trailing_4w
            FROM weekly w
            LEFT JOIN accounts a ON a.WEEK_START = w.WEEK_START
        )
        SELECT * FROM ranked WHERE WEEK_START = ?
        """,
        [str(week_start)],
    )
    if df.empty:
        return {}
    row = df.iloc[0]

    def change(current, baseline):
        if baseline in (None, 0) or pd.isna(baseline):
            return None
        return float((current - baseline) / baseline * 100)

    return {
        "revenue": float(row["REVENUE"]),
        "wow_pct": change(row["REVENUE"], row["PRIOR_REVENUE"]),
        "vs_4w_pct": change(row["REVENUE"], row["TRAILING_4W"]),
        "active_accounts": int(row["ACTIVE_ACCOUNTS"]) if pd.notna(row["ACTIVE_ACCOUNTS"]) else 0,
        "prior_revenue": None if pd.isna(row["PRIOR_REVENUE"]) else float(row["PRIOR_REVENUE"]),
        "trailing_4w": None if pd.isna(row["TRAILING_4W"]) else float(row["TRAILING_4W"]),
    }


@st.cache_data(ttl=TTL)
def revenue_13w(week_start, dimension: str) -> pd.DataFrame:
    """13 weeks of weekly revenue up to and including week_start, by dimension."""
    if dimension not in ("PRODUCT", "SEGMENT"):
        raise ValueError("dimension must be PRODUCT or SEGMENT")
    return _df(
        f"""
        SELECT WEEK_START, {dimension} AS DIMENSION, SUM(REVENUE) AS REVENUE
        FROM {DB}.MARTS.FCT_WEEKLY_REVIEW
        WHERE WEEK_START <= ?
          AND WEEK_START >  DATEADD('week', -13, ?::date)
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        [str(week_start), str(week_start)],
    )


@st.cache_data(ttl=TTL)
def top_movers(week_start, limit: int = 8) -> pd.DataFrame:
    """Largest absolute week-over-week revenue moves by account.

    Ranked on the dollar change, not the percentage: a 400% swing on a $3
    account is not a mover, and a review meeting cares about the dollars.
    """
    return _df(
        f"""
        WITH by_week AS (
            SELECT ACCOUNT_ID,
                   DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
                   SUM(REVENUE)                         AS REVENUE
            FROM {DB}.MARTS.FCT_DAILY_REVENUE
            GROUP BY 1, 2
        ),
        paired AS (
            SELECT ACCOUNT_ID, WEEK_START, REVENUE,
                   LAG(REVENUE) OVER (PARTITION BY ACCOUNT_ID ORDER BY WEEK_START) AS PRIOR_REVENUE
            FROM by_week
        )
        SELECT p.ACCOUNT_ID, a.SEGMENT, a.REGION,
               p.REVENUE, p.PRIOR_REVENUE,
               p.REVENUE - p.PRIOR_REVENUE AS CHANGE_USD,
               CASE WHEN p.PRIOR_REVENUE > 0
                    THEN ROUND((p.REVENUE - p.PRIOR_REVENUE) / p.PRIOR_REVENUE * 100, 1)
               END                          AS CHANGE_PCT
        FROM paired p
        JOIN {DB}.STAGING.STG_ACCOUNTS a ON a.ACCOUNT_ID = p.ACCOUNT_ID
        WHERE p.WEEK_START = ? AND p.PRIOR_REVENUE IS NOT NULL
        ORDER BY ABS(p.REVENUE - p.PRIOR_REVENUE) DESC
        LIMIT ?
        """,
        [str(week_start), limit * 2],
    )


# --------------------------------------------------------------------------
# Data health
# --------------------------------------------------------------------------

@st.cache_data(ttl=TTL)
def page_tier_alerts(week_start) -> pd.DataFrame:
    """Checks precise enough to page: reconciliation, duplicates, unit errors.

    "Unit error" is not a separate model - it is a reconciliation break whose
    usage/ledger ratio is near a power of ten. Splitting it out here gives the
    on-call a named cause rather than a generic mismatch.
    """
    return _df(
        f"""
        WITH recon AS (
            SELECT ACCOUNT_ID,
                   ACTIVITY_DATE                AS EVENT_DATE,
                   NULL                         AS PRODUCT,
                   CASE
                     WHEN LEDGER_REVENUE > 0
                      AND USAGE_REVENUE / NULLIF(LEDGER_REVENUE, 0) >= 100
                     THEN 'unit_error'
                     ELSE 'reconciliation'
                   END                          AS CHECK_NAME,
                   BREAK_KIND                   AS DETAIL,
                   USAGE_REVENUE, LEDGER_REVENUE,
                   ABS_DIFF_USD                 AS IMPACT_USD
            FROM {DB}.DQ.DQ_RECONCILIATION
            WHERE DATE_TRUNC('week', ACTIVITY_DATE)::date = ?
        ),
        dupes AS (
            SELECT ACCOUNT_ID, USAGE_DATE AS EVENT_DATE, PRODUCT,
                   'duplicates'             AS CHECK_NAME,
                   DUPLICATE_KIND           AS DETAIL,
                   REVENUE_AT_GRAIN         AS USAGE_REVENUE,
                   NULL                     AS LEDGER_REVENUE,
                   REVENUE_AT_GRAIN / 2     AS IMPACT_USD
            FROM {DB}.DQ.DQ_DUPLICATES
            WHERE DATE_TRUNC('week', USAGE_DATE)::date = ?
        )
        SELECT * FROM recon
        UNION ALL
        SELECT * FROM dupes
        ORDER BY ABS(IMPACT_USD) DESC NULLS LAST
        """,
        [str(week_start), str(week_start)],
    )


@st.cache_data(ttl=TTL)
def late_arrivals(week_start) -> pd.DataFrame:
    return _df(
        f"""
        SELECT ACCOUNT_ID, USAGE_DATE, LATE_ROW_COUNT, LAG_DAYS,
               FIRST_LOADED_AT, REVENUE_AFFECTED
        FROM {DB}.DQ.DQ_LATE_ARRIVALS
        WHERE DATE_TRUNC('week', USAGE_DATE)::date = ?
        ORDER BY LAG_DAYS DESC
        """,
        [str(week_start)],
    )


@st.cache_data(ttl=TTL)
def review_queue(week_start, threshold: float = REVIEW_Z) -> pd.DataFrame:
    """z-score items for human triage. Never pages - see eval/RESULTS.md."""
    return _df(
        f"""
        SELECT USAGE_DATE, ACCOUNT_ID, PRODUCT, VALUE, EXPECTED, Z, RULE,
               DIRECTION, SPREAD_POINTS
        FROM {DB}.DQ.DQ_ANOMALIES
        WHERE DATE_TRUNC('week', USAGE_DATE)::date = ?
          AND (ABS(Z) > ? OR RULE = 'zero_after_nonzero')
        ORDER BY ABS(Z) DESC NULLS LAST
        """,
        [str(week_start), threshold],
    )


@st.cache_data(ttl=TTL)
def health_status(week_start) -> dict:
    """Red if anything page-tier fired, amber if only review items, else green."""
    page_df = page_tier_alerts(week_start)
    queue_df = review_queue(week_start)
    late_df = late_arrivals(week_start)
    n_page = len(page_df) + len(late_df)
    n_review = len(queue_df)
    if n_page:
        status = "red"
    elif n_review:
        status = "amber"
    else:
        status = "green"
    return {"status": status, "n_page": n_page, "n_review": n_review,
            "n_late": len(late_df), "n_recon": len(page_df)}


@st.cache_data(ttl=TTL)
def daily_series(account_id: str, product: str) -> pd.DataFrame:
    """Daily actual vs expected band for one account-product.

    The band is +/- 2 sigma in log space, converted back to dollars, which is
    why it is asymmetric around the expectation - the model is multiplicative.
    """
    return _df(
        f"""
        SELECT USAGE_DATE, VALUE, EXPECTED_VALUE, Z, SIGMA, SPREAD_POINTS,
               GREATEST(EXP(EXPECTED_Y - 2 * SIGMA) - 1, 0) AS BAND_LOW,
               EXP(EXPECTED_Y + 2 * SIGMA) - 1              AS BAND_HIGH
        FROM {DB}.DQ.DQ_ANOMALY_SCORES_V2
        WHERE ACCOUNT_ID = ? AND PRODUCT = ?
        ORDER BY USAGE_DATE
        """,
        [account_id, product],
    )


@st.cache_data(ttl=TTL)
def history_depth(account_id: str, product: str) -> int:
    """Residual observations backing the spread estimate, in weeks."""
    df = _df(
        f"""
        SELECT MAX(SPREAD_POINTS) AS POINTS
        FROM {DB}.DQ.DQ_ANOMALY_SCORES_V2
        WHERE ACCOUNT_ID = ? AND PRODUCT = ?
        """,
        [account_id, product],
    )
    points = df["POINTS"].iloc[0] if not df.empty else None
    return 0 if points is None or pd.isna(points) else int(points) // 7


# --------------------------------------------------------------------------
# Detector scorecard
# --------------------------------------------------------------------------

@st.cache_data(ttl=TTL)
def scorecard(scenario: str, overall: bool = True) -> pd.DataFrame:
    """Rows from DQ.DETECTOR_SCORECARD. Written by eval/score_detector.py.

    Per-type rows carry no FP or PRECISION by design: a false positive belongs
    to no anomaly type, so a per-type precision would need an invented
    denominator.
    """
    clause = "= '(all)'" if overall else "<> '(all)'"
    return _df(
        f"""
        SELECT DETECTOR, THRESHOLD, ANOMALY_TYPE, TP, FP, FN,
               PRECISION, RECALL, F1, CHANCE_EXPECTED,
               LABELS_IN_SCOPE, SCORED_AT
        FROM {DB}.DQ.DETECTOR_SCORECARD
        WHERE SCENARIO = ? AND ANOMALY_TYPE {clause}
        ORDER BY DETECTOR, THRESHOLD NULLS FIRST, ANOMALY_TYPE
        """,
        [scenario],
    )


@st.cache_data(ttl=TTL)
def scorecard_available() -> bool:
    try:
        _df(f"SELECT 1 FROM {DB}.DQ.DETECTOR_SCORECARD LIMIT 1")
        return True
    except Exception:
        return False
