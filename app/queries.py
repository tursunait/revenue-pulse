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

Two rules this module enforces rather than leaving to the UI:

* **Partial weeks never enter a comparison.** The data window ends mid-week, so
  the newest week can hold a single day. Every week-over-week and
  trailing-average calculation is built from complete weeks only, and
  `assert_comparable` makes a partial-vs-complete comparison raise rather than
  silently return a number like -84%.
* **Reported vs Adjusted.** "Adjusted" drops account-days carrying an open
  amount-affecting page-tier issue. Late arrivals are deliberately NOT dropped:
  their values are correct and only `loaded_at` moved, so removing them would
  delete good revenue.

Cache TTL is 15 minutes. The warehouse auto-suspends after 60s, so every cache
miss pays a cold-start; a short TTL keeps the app responsive without serving
stale numbers through a working session.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

DB = "REVENUE_PULSE"
TTL = 900

#: |z| above which an item enters the review queue. Matches the recommendation
#: in eval/RESULTS.md: page on |z|>5 only when no ledger exists, review from 3.
REVIEW_Z = 3.0

#: Weeks of history detector v2 needs before it will score an account-product.
MIN_HISTORY_WEEKS = 13


#: Set by a local tool that has its own connection. Inside Streamlit in
#: Snowflake this stays None and the active Snowpark session is used.
_SESSION_OVERRIDE = None


def use_session(adapter) -> None:
    """Point this module at a connection other than the Snowpark session.

    scripts/generate_summary.py runs locally, where there is no active Snowpark
    session (and Snowpark need not even be installed). Rather than restating
    these queries in that script - and letting the two drift - it injects an
    adapter exposing the one method this module uses: .sql(text, params) with a
    .to_pandas() on the result.
    """
    global _SESSION_OVERRIDE
    _SESSION_OVERRIDE = adapter


def session():
    if _SESSION_OVERRIDE is not None:
        return _SESSION_OVERRIDE
    # Imported lazily so this module can be imported outside Snowflake.
    from snowflake.snowpark.context import get_active_session
    return get_active_session()


def _df(sql: str, params: list | None = None) -> pd.DataFrame:
    result = session().sql(sql, params=params) if params else session().sql(sql)
    return result.to_pandas()


# --------------------------------------------------------------------------
# Weekly review
# --------------------------------------------------------------------------

DAYS_IN_WEEK = 7


class NotComparable(RuntimeError):
    """Raised when a partial week would be compared against a different week."""


@st.cache_data(ttl=TTL)
def week_calendar() -> pd.DataFrame:
    """Every week with how many of its 7 days the data actually covers.

    Completeness is measured from the facts, not assumed from the calendar:
    the generator's window ends on a fixed date, so the newest week is usually
    partial and the comparison logic has to know it.
    """
    return _df(f"""
        SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
               COUNT(DISTINCT USAGE_DATE)           AS DAYS_PRESENT,
               COUNT(DISTINCT USAGE_DATE) = {DAYS_IN_WEEK} AS IS_COMPLETE,
               MIN(USAGE_DATE)                      AS FIRST_DAY,
               MAX(USAGE_DATE)                      AS LAST_DAY
        FROM {DB}.MARTS.FCT_DAILY_REVENUE
        GROUP BY 1
        ORDER BY 1 DESC
        """)


@st.cache_data(ttl=TTL)
def weeks() -> list:
    """All week_start values, newest first."""
    return week_calendar()["WEEK_START"].tolist()


@st.cache_data(ttl=TTL)
def complete_weeks() -> list:
    cal = week_calendar()
    return cal.loc[cal["IS_COMPLETE"], "WEEK_START"].tolist()


@st.cache_data(ttl=TTL)
def default_week():
    """The newest COMPLETE week - never the partial one at the window edge."""
    done = complete_weeks()
    return done[0] if done else (weeks()[0] if weeks() else None)


@st.cache_data(ttl=TTL)
def week_days() -> dict:
    cal = week_calendar()
    return dict(zip(cal["WEEK_START"], cal["DAYS_PRESENT"]))


def week_label(week_start) -> str:
    days = week_days().get(week_start)
    if days is None:
        return str(week_start)
    if days == DAYS_IN_WEEK:
        return str(week_start)
    return f"{week_start} — partial ({days} of {DAYS_IN_WEEK} days)"


def is_partial(week_start) -> bool:
    return week_days().get(week_start, DAYS_IN_WEEK) != DAYS_IN_WEEK


def assert_comparable(week_a, week_b) -> None:
    """Guard: refuse to compare weeks covering different numbers of days.

    The SQL already restricts comparisons to complete weeks, so this should
    never fire. It exists because the failure it prevents is silent - a partial
    week against a full one produced a plausible-looking -84% that was purely
    an artefact of counting one day against seven.
    """
    days = week_days()
    a, b = days.get(week_a), days.get(week_b)
    if a is None or b is None:
        return
    if a != b:
        raise NotComparable(
            f"refusing to compare {week_a} ({a} days) with {week_b} ({b} days)"
        )


@st.cache_data(ttl=TTL)
def week_kpis(week_start) -> dict:
    """Revenue, WoW %, vs trailing 4-week average, active accounts.

    The comparison series is built from COMPLETE weeks only, so a partial week
    is never a baseline and never a comparand. A partial week still reports its
    own revenue and account count - those are facts about the days that exist -
    but its WoW and trailing figures come back None with a reason.

    Week-level ratios are computed from week-level totals rather than averaging
    the per-segment percentages, which would weight the smallest segment the
    same as the largest.
    """
    partial = is_partial(week_start)

    own = _df(
        f"""
        SELECT SUM(REVENUE)                AS REVENUE,
               COUNT(DISTINCT ACCOUNT_ID)  AS ACTIVE_ACCOUNTS,
               COUNT(DISTINCT USAGE_DATE)  AS DAYS_PRESENT
        FROM {DB}.MARTS.FCT_DAILY_REVENUE
        WHERE DATE_TRUNC('week', USAGE_DATE)::date = ?
        """,
        [str(week_start)],
    )
    if own.empty or pd.isna(own["REVENUE"].iloc[0]):
        return {}
    row = own.iloc[0]

    base = {
        "revenue": float(row["REVENUE"]),
        "active_accounts": int(row["ACTIVE_ACCOUNTS"]),
        "days_present": int(row["DAYS_PRESENT"]),
        "is_partial": bool(partial),
        "wow_pct": None,
        "vs_4w_pct": None,
        "prior_revenue": None,
        "trailing_4w": None,
        "prior_week": None,
        "reason": None,
    }

    if partial:
        base["reason"] = (
            f"This week covers {base['days_present']} of {DAYS_IN_WEEK} days. "
            "Comparisons are withheld rather than shown against a full week."
        )
        return base

    comp = _df(
        f"""
        WITH by_week AS (
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
                   SUM(REVENUE)                        AS REVENUE,
                   COUNT(DISTINCT USAGE_DATE)          AS DAYS_PRESENT
            FROM {DB}.MARTS.FCT_DAILY_REVENUE
            GROUP BY 1
        ),
        -- Complete weeks only. This is the single place the partial week is
        -- excluded from every downstream comparison.
        full_weeks AS (
            SELECT WEEK_START, REVENUE FROM by_week WHERE DAYS_PRESENT = {DAYS_IN_WEEK}
        ),
        ranked AS (
            SELECT WEEK_START, REVENUE,
                   LAG(WEEK_START) OVER (ORDER BY WEEK_START) AS PRIOR_WEEK,
                   LAG(REVENUE)    OVER (ORDER BY WEEK_START) AS PRIOR_REVENUE,
                   AVG(REVENUE)    OVER (ORDER BY WEEK_START
                                         ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING)
                                                              AS TRAILING_4W
            FROM full_weeks
        )
        SELECT * FROM ranked WHERE WEEK_START = ?
        """,
        [str(week_start)],
    )
    if comp.empty:
        base["reason"] = "No complete-week comparison available."
        return base
    c = comp.iloc[0]

    if pd.notna(c["PRIOR_WEEK"]):
        # Both are complete by construction; this asserts it rather than trusting it.
        assert_comparable(week_start, c["PRIOR_WEEK"])
        base["prior_week"] = c["PRIOR_WEEK"]

    def change(current, baseline):
        if baseline is None or pd.isna(baseline) or baseline == 0:
            return None
        return float((current - baseline) / baseline * 100)

    base["prior_revenue"] = (
        None if pd.isna(c["PRIOR_REVENUE"]) else float(c["PRIOR_REVENUE"])
    )
    base["trailing_4w"] = None if pd.isna(c["TRAILING_4W"]) else float(c["TRAILING_4W"])
    base["wow_pct"] = change(base["revenue"], base["prior_revenue"])
    base["vs_4w_pct"] = change(base["revenue"], base["trailing_4w"])
    return base


# --------------------------------------------------------------------------
# Open page-tier issues, and the Reported / Adjusted split
# --------------------------------------------------------------------------

#: Account-days carrying an amount-affecting page-tier issue. Reconciliation
#: breaks and duplicate loads distort the revenue figure; late arrivals do not
#: (their values are correct, only loaded_at moved), so they are excluded here
#: and would otherwise delete good revenue from the adjusted view.
_FLAGGED_SQL = f"""
    SELECT ACCOUNT_ID, ACTIVITY_DATE AS EVENT_DATE,
           CASE
             WHEN LEDGER_REVENUE > 0
              AND USAGE_REVENUE / NULLIF(LEDGER_REVENUE, 0) >= 100 THEN 'unit_error'
             ELSE 'reconciliation'
           END AS ISSUE
    FROM {DB}.DQ.DQ_RECONCILIATION
    UNION
    SELECT DISTINCT ACCOUNT_ID, USAGE_DATE AS EVENT_DATE, 'duplicates' AS ISSUE
    FROM {DB}.DQ.DQ_DUPLICATES
"""


@st.cache_data(ttl=TTL)
def flagged_account_days(week_start=None) -> pd.DataFrame:
    """Account-days excluded in Adjusted mode, with the revenue each carries."""
    where = "WHERE DATE_TRUNC('week', f.EVENT_DATE)::date = ?" if week_start else ""
    params = [str(week_start)] if week_start else None
    return _df(
        f"""
        WITH flagged AS ({_FLAGGED_SQL}),
        grouped AS (
            SELECT ACCOUNT_ID, EVENT_DATE,
                   LISTAGG(DISTINCT ISSUE, ', ') AS ISSUES
            FROM flagged GROUP BY 1, 2
        )
        SELECT f.ACCOUNT_ID, f.EVENT_DATE, f.ISSUES,
               COALESCE(SUM(d.REVENUE), 0) AS REVENUE_AT_RISK,
               DATE_TRUNC('week', f.EVENT_DATE)::date AS WEEK_START
        FROM grouped f
        LEFT JOIN {DB}.MARTS.FCT_DAILY_REVENUE d
               ON d.ACCOUNT_ID = f.ACCOUNT_ID AND d.USAGE_DATE = f.EVENT_DATE
        {where}
        GROUP BY 1, 2, 3, 5
        ORDER BY REVENUE_AT_RISK DESC
        """,
        params,
    )


@st.cache_data(ttl=TTL)
def under_review(week_start) -> dict:
    """How much of the selected week's revenue sits on a flagged account-day."""
    df = flagged_account_days(week_start)
    if df.empty:
        return {"amount": 0.0, "account_days": 0, "issues": ""}
    issues = sorted({i.strip() for row in df["ISSUES"] for i in str(row).split(",")})
    return {
        "amount": float(df["REVENUE_AT_RISK"].sum()),
        "account_days": int(len(df)),
        "issues": ", ".join(issues),
    }


@st.cache_data(ttl=TTL)
def week_issue_markers(week_start) -> pd.DataFrame:
    """Per complete week in the 13-week window: what is under review, and how much.

    Returns the revenue at risk and its share of the week so the caller can
    apply the same materiality rule as the banner. Without the amount, every
    week with any open item looked equally alarming - 10 of 13 weeks carried a
    red marker, which is the same as none of them carrying one.
    """
    return _df(
        f"""
        WITH flagged AS ({_FLAGGED_SQL}),
        at_risk AS (
            SELECT f.ACCOUNT_ID, f.EVENT_DATE,
                   LISTAGG(DISTINCT f.ISSUE, ', ') AS ISSUES
            FROM flagged f GROUP BY 1, 2
        ),
        day_revenue AS (
            SELECT a.ACCOUNT_ID, a.EVENT_DATE, a.ISSUES,
                   COALESCE(SUM(d.REVENUE), 0) AS REVENUE_AT_RISK
            FROM at_risk a
            LEFT JOIN {DB}.MARTS.FCT_DAILY_REVENUE d
                   ON d.ACCOUNT_ID = a.ACCOUNT_ID AND d.USAGE_DATE = a.EVENT_DATE
            GROUP BY 1, 2, 3
        ),
        per_week AS (
            SELECT DATE_TRUNC('week', EVENT_DATE)::date AS WEEK_START,
                   COUNT(*)                             AS ACCOUNT_DAYS,
                   LISTAGG(DISTINCT ISSUES, ', ')       AS ISSUES,
                   SUM(REVENUE_AT_RISK)                 AS REVENUE_AT_RISK
            FROM day_revenue GROUP BY 1
        ),
        week_totals AS (
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
                   SUM(REVENUE)                         AS WEEK_REVENUE,
                   COUNT(DISTINCT USAGE_DATE)           AS DAYS_PRESENT
            FROM {DB}.MARTS.FCT_DAILY_REVENUE GROUP BY 1
        )
        SELECT t.WEEK_START, p.ACCOUNT_DAYS, p.ISSUES,
               p.REVENUE_AT_RISK, t.WEEK_REVENUE,
               CASE WHEN t.WEEK_REVENUE > 0
                    THEN p.REVENUE_AT_RISK / t.WEEK_REVENUE * 100 END AS SHARE_PCT
        FROM week_totals t
        JOIN per_week p ON p.WEEK_START = t.WEEK_START
        WHERE t.DAYS_PRESENT = {DAYS_IN_WEEK}
          AND t.WEEK_START <= ?
          AND t.WEEK_START >  DATEADD('week', -13, ?::date)
        ORDER BY t.WEEK_START
        """,
        [str(week_start), str(week_start)],
    )


@st.cache_data(ttl=TTL)
def revenue_13w(week_start, dimension: str, mode: str = "reported") -> pd.DataFrame:
    """13 complete weeks of revenue by product or segment.

    Partial weeks are excluded entirely - a one-day week plotted beside
    seven-day weeks reads as a collapse in revenue when nothing has happened.

    mode="adjusted" drops every account-day carrying an open amount-affecting
    page-tier issue. The whole account-day goes, not just the excess, because
    once a day's figure is known to be wrong there is no defensible number left
    on it - see DECISIONS.md part 8 for why that bluntness is deliberate.
    """
    if dimension not in ("PRODUCT", "SEGMENT"):
        raise ValueError("dimension must be PRODUCT or SEGMENT")
    if mode not in ("reported", "adjusted"):
        raise ValueError("mode must be reported or adjusted")

    exclude = (
        f"""
        AND NOT EXISTS (
            SELECT 1 FROM flagged f
            WHERE f.ACCOUNT_ID = d.ACCOUNT_ID AND f.EVENT_DATE = d.USAGE_DATE
        )
    """
        if mode == "adjusted"
        else ""
    )

    return _df(
        f"""
        WITH flagged AS ({_FLAGGED_SQL}),
        complete AS (
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START
            FROM {DB}.MARTS.FCT_DAILY_REVENUE
            GROUP BY 1
            HAVING COUNT(DISTINCT USAGE_DATE) = {DAYS_IN_WEEK}
        )
        SELECT DATE_TRUNC('week', d.USAGE_DATE)::date AS WEEK_START,
               d.{dimension}                          AS DIMENSION,
               SUM(d.REVENUE)                         AS REVENUE
        FROM {DB}.MARTS.FCT_DAILY_REVENUE d
        JOIN complete c ON c.WEEK_START = DATE_TRUNC('week', d.USAGE_DATE)::date
        WHERE DATE_TRUNC('week', d.USAGE_DATE)::date <= ?
          AND DATE_TRUNC('week', d.USAGE_DATE)::date >  DATEADD('week', -13, ?::date)
          {exclude}
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        [str(week_start), str(week_start)],
    )


@st.cache_data(ttl=TTL)
def revenue_13w_totals(week_start, mode: str = "reported") -> pd.DataFrame:
    """Week totals for the same window, used to verify Reported vs Adjusted."""
    df = revenue_13w(week_start, "PRODUCT", mode)
    if df.empty:
        return df
    return df.groupby("WEEK_START", as_index=False)["REVENUE"].sum()


@st.cache_data(ttl=TTL)
def top_movers(week_start, limit: int = 8, basis: str = "reported") -> pd.DataFrame:
    """Top `limit` risers AND top `limit` fallers, week over week.

    `basis="excluding_under_review"` drops account-days carrying an open
    amount-affecting issue from BOTH weeks before ranking. That matters because
    a mover list answers "what changed in the business", and an account whose
    prior week was inflated by a unit error produces a huge fake fall that
    crowds the real movers out of the list entirely - see DECISIONS.md part 11.

    The two directions are ranked separately. Taking the top 2N by absolute
    change and splitting gave unequal lists - 7 up against 8 down - which reads
    as a finding about the business when it is an artefact of the cut.

    Built from complete weeks only, so the prior week is always seven days.
    Ranked on dollar change rather than percentage: a 400% swing on a $3
    account is not a mover a review meeting cares about.
    """
    if basis not in ("reported", "excluding_under_review"):
        raise ValueError("basis must be reported or excluding_under_review")

    exclude = """
        AND NOT EXISTS (
            SELECT 1 FROM flagged f
            WHERE f.ACCOUNT_ID = d.ACCOUNT_ID AND f.EVENT_DATE = d.USAGE_DATE
        )
    """ if basis == "excluding_under_review" else ""

    return _df(
        f"""
        WITH flagged AS ({_FLAGGED_SQL}),
        by_week AS (
            SELECT d.ACCOUNT_ID,
                   DATE_TRUNC('week', d.USAGE_DATE)::date AS WEEK_START,
                   SUM(d.REVENUE)                         AS REVENUE,
                   COUNT(DISTINCT d.USAGE_DATE)           AS DAYS_PRESENT
            FROM {DB}.MARTS.FCT_DAILY_REVENUE d
            WHERE 1 = 1 {exclude}
            GROUP BY 1, 2
        ),
        complete_weeks AS (
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START
            FROM {DB}.MARTS.FCT_DAILY_REVENUE
            GROUP BY 1 HAVING COUNT(DISTINCT USAGE_DATE) = {DAYS_IN_WEEK}
        ),
        complete AS (
            SELECT b.* FROM by_week b
            JOIN complete_weeks w ON w.WEEK_START = b.WEEK_START
        ),
        paired AS (
            SELECT ACCOUNT_ID, WEEK_START, REVENUE,
                   LAG(REVENUE) OVER (PARTITION BY ACCOUNT_ID ORDER BY WEEK_START)
                       AS PRIOR_REVENUE
            FROM complete
        ),
        changed AS (
            SELECT ACCOUNT_ID, WEEK_START, REVENUE, PRIOR_REVENUE,
                   REVENUE - PRIOR_REVENUE AS CHANGE_USD
            FROM paired
            WHERE WEEK_START = ? AND PRIOR_REVENUE IS NOT NULL
        )
        SELECT c.ACCOUNT_ID, a.SEGMENT, a.REGION,
               c.REVENUE, c.PRIOR_REVENUE, c.CHANGE_USD,
               CASE WHEN c.PRIOR_REVENUE > 0
                    THEN ROUND(c.CHANGE_USD / c.PRIOR_REVENUE * 100, 1) END AS CHANGE_PCT,
               CASE WHEN c.CHANGE_USD > 0 THEN 'up' ELSE 'down' END          AS DIRECTION
        FROM changed c
        JOIN {DB}.STAGING.STG_ACCOUNTS a ON a.ACCOUNT_ID = c.ACCOUNT_ID
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY CASE WHEN c.CHANGE_USD > 0 THEN 'up' ELSE 'down' END
            ORDER BY ABS(c.CHANGE_USD) DESC
        ) <= ?
        ORDER BY ABS(c.CHANGE_USD) DESC
        """,
        [str(week_start), limit],
    )


@st.cache_data(ttl=TTL)
def mover_counts(week_start) -> dict:
    """How many accounts moved in each direction at all - for the caption."""
    df = _df(
        f"""
        WITH by_week AS (
            SELECT ACCOUNT_ID,
                   DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
                   SUM(REVENUE)                         AS REVENUE,
                   COUNT(DISTINCT USAGE_DATE)           AS DAYS_PRESENT
            FROM {DB}.MARTS.FCT_DAILY_REVENUE GROUP BY 1, 2
        ),
        complete_weeks AS (
            SELECT WEEK_START FROM by_week
            GROUP BY 1 HAVING MAX(DAYS_PRESENT) = {DAYS_IN_WEEK}
        ),
        paired AS (
            SELECT b.ACCOUNT_ID, b.WEEK_START, b.REVENUE,
                   LAG(b.REVENUE) OVER (PARTITION BY b.ACCOUNT_ID ORDER BY b.WEEK_START)
                       AS PRIOR_REVENUE
            FROM by_week b JOIN complete_weeks w ON w.WEEK_START = b.WEEK_START
        )
        SELECT SUM(CASE WHEN REVENUE - PRIOR_REVENUE > 0 THEN 1 ELSE 0 END) AS N_UP,
               SUM(CASE WHEN REVENUE - PRIOR_REVENUE < 0 THEN 1 ELSE 0 END) AS N_DOWN
        FROM paired
        WHERE WEEK_START = ? AND PRIOR_REVENUE IS NOT NULL
        """,
        [str(week_start)],
    )
    if df.empty:
        return {"up": 0, "down": 0}
    return {"up": int(df["N_UP"].iloc[0] or 0), "down": int(df["N_DOWN"].iloc[0] or 0)}


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
    return {
        "status": status,
        "n_page": n_page,
        "n_review": n_review,
        "n_late": len(late_df),
        "n_recon": len(page_df),
    }


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


# --------------------------------------------------------------------------
# AI summary
# --------------------------------------------------------------------------

@st.cache_data(ttl=TTL)
def ai_summary(week_start) -> dict | None:
    """The stored summary for a week, or None.

    Written by scripts/generate_summary.py. The app only reads it - generation
    happens outside Snowflake because Cortex AI_COMPLETE is blocked on a trial
    account (DECISIONS.md part 10).
    """
    try:
        df = _df(
            f"""
            SELECT TO_VARCHAR(WEEK_START, 'YYYY-MM-DD') AS WEEK_START,
                   MODEL, PROMPT_HASH, INPUT_JSON, SUMMARY,
                   TO_VARCHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI') AS CREATED_AT
            FROM {DB}.DQ.AI_SUMMARY
            WHERE WEEK_START = TO_DATE(?)
            ORDER BY CREATED_AT DESC
            LIMIT 1
            """,
            [str(week_start)],
        )
    except Exception:
        return None          # table not created yet
    if df.empty:
        return None
    return df.iloc[0].to_dict()
