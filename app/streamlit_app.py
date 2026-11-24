"""Revenue Pulse - Streamlit in Snowflake.

Three pages: Weekly Review, Data Health, AI Summary (Step 6 placeholder).

Runs entirely inside Snowflake. The session comes from get_active_session(),
so there are no credentials in this app and nothing reads .env. Only packages
from Snowflake's Anaconda channel are imported: streamlit, pandas, altair.

Navigation is a sidebar radio rather than st.navigation / st.switch_page, so
the health banner on page 1 can jump to page 2 by setting session state - which
works the same way on every Streamlit version Snowflake has shipped.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

import queries as q
from components import (
    direction_of,
    health_banner,
    load_styles,
    money,
    note,
    pct,
    render_metric_card,
    status_badge,
)

st.set_page_config(page_title="Revenue Pulse", page_icon="📈", layout="wide")
load_styles()

PAGES = ["Weekly Review", "Data Health", "AI Summary"]
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

HEALTH_COPY = {
    "red": ("Action needed", "A page-tier check fired this week. These compare two "
                             "independent sources, so a break is a real discrepancy."),
    "amber": ("Review queue only", "No page-tier breaks. Statistical items are waiting "
                                   "for a human to judge."),
    "green": ("All clear", "No page-tier breaks and nothing in the review queue."),
}


# --------------------------------------------------------------------------
# Page 1 - Weekly Review
# --------------------------------------------------------------------------

def page_weekly_review(week_start) -> None:
    st.title("Weekly Review")

    health = q.health_status(week_start)
    headline, detail = HEALTH_COPY[health["status"]]
    banner_col, button_col = st.columns([5, 1])
    with banner_col:
        health_banner(
            health["status"],
            f"Data health — {headline}",
            f"{detail}  "
            f"({health['n_page']} page-tier, {health['n_review']} in review queue)",
        )
    with button_col:
        if st.button("Open Data Health", use_container_width=True):
            st.session_state.page = "Data Health"
            st.session_state.health_week = week_start
            st.rerun()

    kpis = q.week_kpis(week_start)
    if not kpis:
        st.warning(f"No weekly data for {week_start}.")
        return

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        render_metric_card("Revenue this week", money(kpis["revenue"]),
                           help_text=f"Week beginning {week_start}")
    with c2:
        render_metric_card(
            "Week over week", pct(kpis["wow_pct"]),
            delta=f"from {money(kpis['prior_revenue'])}",
            direction=direction_of(kpis["wow_pct"]),
            help_text="Against the previous week",
        )
    with c3:
        render_metric_card(
            "vs 4-week average", pct(kpis["vs_4w_pct"]),
            delta=f"avg {money(kpis['trailing_4w'])}",
            direction=direction_of(kpis["vs_4w_pct"]),
            help_text="Trailing 4 weeks, this week excluded",
        )
    with c4:
        render_metric_card("Active accounts", f"{kpis['active_accounts']:,}",
                           help_text="Billed at least once this week")

    st.markdown("### Revenue trend, last 13 weeks")
    by_product = q.revenue_13w(week_start, "PRODUCT")
    by_segment = q.revenue_13w(week_start, "SEGMENT")
    left, right = st.columns(2)
    with left:
        st.caption("By product")
        _trend_chart(by_product)
    with right:
        st.caption("By segment")
        _trend_chart(by_segment)

    st.markdown("### Top movers, week over week")
    movers = q.top_movers(week_start)
    if movers.empty:
        note("No prior week to compare against.")
        return
    up = movers[movers["CHANGE_USD"] > 0].head(8)
    down = movers[movers["CHANGE_USD"] < 0].head(8)
    left, right = st.columns(2)
    with left:
        st.caption("Up")
        _movers_table(up)
    with right:
        st.caption("Down")
        _movers_table(down)
    note("Ranked by dollar change, not percentage — a large percentage move on a "
         "very small account is not a mover worth a review meeting.")


def _trend_chart(df: pd.DataFrame) -> None:
    if df.empty:
        note("No data for this window.")
        return
    chart = (
        alt.Chart(df)
        .mark_line(point=True)
        .encode(
            x=alt.X("WEEK_START:T", title=None),
            y=alt.Y("REVENUE:Q", title="Revenue", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("DIMENSION:N", title=None,
                            legend=alt.Legend(orient="bottom")),
            tooltip=[
                alt.Tooltip("WEEK_START:T", title="Week"),
                alt.Tooltip("DIMENSION:N", title=""),
                alt.Tooltip("REVENUE:Q", title="Revenue", format="$,.0f"),
            ],
        )
        .properties(height=260)
    )
    st.altair_chart(chart, use_container_width=True)


def _movers_table(df: pd.DataFrame) -> None:
    if df.empty:
        note("None.")
        return
    shown = df[["ACCOUNT_ID", "SEGMENT", "REVENUE", "CHANGE_USD", "CHANGE_PCT"]].copy()
    shown.columns = ["Account", "Segment", "Revenue", "Change ($)", "Change (%)"]
    st.dataframe(shown, hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------
# Page 2 - Data Health
# --------------------------------------------------------------------------

def page_data_health(week_start) -> None:
    st.title("Data Health")

    health = q.health_status(week_start)
    headline, detail = HEALTH_COPY[health["status"]]
    health_banner(health["status"], f"Week of {week_start} — {headline}", detail)

    st.markdown("### Page-tier alerts")
    note("Checks that compare two independent sources. Precision is high enough that "
         "one of these firing means a real discrepancy, so these are the ones that "
         "should wake someone.")
    alerts = q.page_tier_alerts(week_start)
    late = q.late_arrivals(week_start)

    if alerts.empty and late.empty:
        status_badge("green", "No page-tier alerts")
    else:
        if not alerts.empty:
            shown = alerts[["EVENT_DATE", "ACCOUNT_ID", "PRODUCT", "CHECK_NAME",
                            "DETAIL", "USAGE_REVENUE", "LEDGER_REVENUE", "IMPACT_USD"]].copy()
            shown.columns = ["Date", "Account", "Product", "Check", "Detail",
                             "Usage $", "Ledger $", "Impact $"]
            st.dataframe(shown, hide_index=True, use_container_width=True)
        if not late.empty:
            st.caption("Late arrivals — values correct, freshness fault")
            shown = late[["USAGE_DATE", "ACCOUNT_ID", "LATE_ROW_COUNT", "LAG_DAYS",
                          "REVENUE_AFFECTED"]].copy()
            shown.columns = ["Date", "Account", "Late rows", "Lag (days)", "Revenue $"]
            st.dataframe(shown, hide_index=True, use_container_width=True)

    st.markdown(f"### Review queue — z-score |z| > {q.REVIEW_Z:g}")
    note("Statistical items. These compare a series against its own history, so an "
         "item means 'unusual', not 'wrong'. Deliberately not wired to a pager.")
    queue = q.review_queue(week_start)
    if queue.empty:
        status_badge("green", "Review queue empty")
    else:
        shown = queue[["USAGE_DATE", "ACCOUNT_ID", "PRODUCT", "VALUE", "EXPECTED",
                       "Z", "RULE"]].copy()
        shown.columns = ["Date", "Account", "Product", "Actual $", "Expected $",
                         "z", "Rule"]
        st.dataframe(shown, hide_index=True, use_container_width=True)

    _inspector(week_start, alerts, queue)
    _scorecard_section()


def _inspector(week_start, alerts: pd.DataFrame, queue: pd.DataFrame) -> None:
    """Pick an item and see that account-product's daily series."""
    st.markdown("### Inspect an item")

    options: list[tuple[str, str, object]] = []
    for _, r in queue.iterrows():
        options.append((r["ACCOUNT_ID"], r["PRODUCT"], r["USAGE_DATE"]))
    for _, r in alerts.iterrows():
        if r["PRODUCT"]:
            options.append((r["ACCOUNT_ID"], r["PRODUCT"], r["EVENT_DATE"]))

    if not options:
        note("Nothing flagged this week to inspect.")
        return

    labels = [f"{a} · {p} · {d}" for a, p, d in options]
    picked = st.selectbox("Flagged item", labels, index=0)
    account_id, product, flagged_on = options[labels.index(picked)]

    weeks_of_history = q.history_depth(account_id, product)
    if weeks_of_history < q.MIN_HISTORY_WEEKS:
        status_badge("amber", "Not enough history")
        note(
            f"{account_id} · {product} has about {weeks_of_history} weeks of residual "
            f"history; the detector needs {q.MIN_HISTORY_WEEKS} before it will score. "
            "Until then this account-product is unscored rather than scored badly — "
            "see the cohort-baseline note in eval/RESULTS.md."
        )
        return

    series = q.daily_series(account_id, product)
    if series.empty:
        note("No scored series for this account-product.")
        return

    st.caption(f"{account_id} · {product} — actual against the expected band "
               f"(±2σ, asymmetric because the model is multiplicative)")
    _series_chart(series, flagged_on)


def _series_chart(df: pd.DataFrame, flagged_on) -> None:
    band = (
        alt.Chart(df)
        .mark_area(opacity=0.18)
        .encode(
            x=alt.X("USAGE_DATE:T", title=None),
            y=alt.Y("BAND_LOW:Q", title="Revenue", axis=alt.Axis(format="$,.0f")),
            y2="BAND_HIGH:Q",
        )
    )
    expected = (
        alt.Chart(df).mark_line(strokeDash=[4, 3])
        .encode(x="USAGE_DATE:T", y="EXPECTED_VALUE:Q")
    )
    actual = (
        alt.Chart(df).mark_line()
        .encode(
            x="USAGE_DATE:T",
            y="VALUE:Q",
            tooltip=[
                alt.Tooltip("USAGE_DATE:T", title="Date"),
                alt.Tooltip("VALUE:Q", title="Actual", format="$,.2f"),
                alt.Tooltip("EXPECTED_VALUE:Q", title="Expected", format="$,.2f"),
                alt.Tooltip("Z:Q", title="z", format=".2f"),
            ],
        )
    )
    layers = [band, expected, actual]

    marked = df[pd.to_datetime(df["USAGE_DATE"]) == pd.to_datetime(flagged_on)]
    if not marked.empty:
        layers.append(
            alt.Chart(marked).mark_point(size=160, filled=True, color="#dc3545")
            .encode(x="USAGE_DATE:T", y="VALUE:Q")
        )
    st.altair_chart(alt.layer(*layers).properties(height=320),
                    use_container_width=True)


def _scorecard_section() -> None:
    st.markdown("### Detector scorecard")
    note("Read from DQ.DETECTOR_SCORECARD, which eval/score_detector.py writes. "
         "This app never reads RAW.ANOMALY_LABELS — the scorecard exists so detector "
         "quality can be shown without it.")

    if not q.scorecard_available():
        status_badge("grey", "Scorecard not built")
        note("Run `python eval/score_detector.py` to populate DQ.DETECTOR_SCORECARD.")
        return

    scenario_label = st.radio(
        "Scenario",
        ["A: ledger available", "B: no daily ledger"],
        horizontal=True,
    )
    scenario = "A" if scenario_label.startswith("A") else "B"

    overall = q.scorecard(scenario, overall=True)
    if overall.empty:
        note("No scorecard rows for this scenario.")
        return

    shown = overall[["DETECTOR", "THRESHOLD", "TP", "FP", "FN",
                     "PRECISION", "RECALL", "F1", "CHANCE_EXPECTED"]].copy()
    shown.columns = ["Detector", "Threshold", "TP", "FP", "FN",
                     "Precision", "Recall", "F1", "Chance"]
    st.dataframe(shown, hide_index=True, use_container_width=True)

    with st.expander("Recall by anomaly type"):
        per_type = q.scorecard(scenario, overall=False)
        if per_type.empty:
            note("No per-type rows.")
        else:
            pivot = per_type.pivot_table(
                index="ANOMALY_TYPE", columns=["DETECTOR", "THRESHOLD"],
                values="RECALL", aggfunc="max", dropna=False,
            )
            st.dataframe(pivot, use_container_width=True)
            note("Precision is absent per type on purpose: a false positive belongs to "
                 "no anomaly type, so a per-type precision would need an invented "
                 "denominator. Compare recall against the chance column above.")

    if scenario == "B":
        note("Scenario B is the realistic case when finance closes monthly. "
             "ledger_mismatch is uncoverable there — the usage side is clean and only "
             "the booked figure moved, so nothing observable remains.")


# --------------------------------------------------------------------------
# Page 3 - AI Summary
# --------------------------------------------------------------------------

def page_ai_summary(week_start) -> None:
    st.title("AI Summary")
    status_badge("grey", "Step 6")
    st.markdown(
        f"""
Narrative summary of the week beginning **{week_start}** will be generated here
with Snowflake Cortex `AI_COMPLETE`.

Planned inputs, all already materialised:

- `MARTS.FCT_WEEKLY_REVIEW` — revenue, week-over-week, trailing-4-week comparison
- top movers up and down, as shown on the Weekly Review page
- `DQ` page-tier alerts and the review queue for the same week

Intended output: a short brief naming what moved, which accounts drove it, and
whether any of the movement is explained by a data-quality fault rather than
real consumption.
        """
    )
    st.info(
        "AI_COMPLETE is not available on a Snowflake trial account — every model "
        "returns `AI function _COMPLETE_WITH_PROMPT_HISTORY_LLM is not available for "
        "trial accounts`. This page needs a non-trial account, or a graceful "
        "fallback that renders the same brief from a template when the call fails.",
        icon="ℹ️",
    )


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------

def main() -> None:
    st.sidebar.title("Revenue Pulse")
    st.session_state.page = st.sidebar.radio(
        "Page", PAGES, index=PAGES.index(st.session_state.page)
    )

    available = q.weeks()
    if not available:
        st.error("No weeks found in MARTS.FCT_WEEKLY_REVIEW. Has dbt been run?")
        return

    default_week = st.session_state.get("health_week", available[0])
    index = available.index(default_week) if default_week in available else 0
    week_start = st.sidebar.selectbox("Week beginning", available, index=index)
    st.session_state.health_week = week_start

    st.sidebar.caption(
        f"{len(available)} weeks available · warehouse auto-suspends after 60s, "
        "so the first query of a session is slow."
    )

    if st.session_state.page == "Weekly Review":
        page_weekly_review(week_start)
    elif st.session_state.page == "Data Health":
        page_data_health(week_start)
    else:
        page_ai_summary(week_start)


main()
