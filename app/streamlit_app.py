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

import json

import altair as alt
import pandas as pd
import streamlit as st

import queries as q
from components import (
    direction_of,
    health_banner,
    load_styles,
    md,
    md_caption,
    md_info,
    as_int,
    md_money,
    md_money_cents,
    md_warning,
    money,
    money_cents,
    note,
    num,
    pct,
    plain_money,
    plain_money_cents,
    plural,
    render_metric_card,
    share_pct,
    signed_money,
    signed_pct,
    status_badge,
    styled_table,
)
from claims import MATERIAL_ABS_USD, MATERIAL_SHARE_PCT, check_claims
from verifier import verify

st.set_page_config(page_title="Revenue Pulse", page_icon="📈", layout="wide")
load_styles()

PAGES = ["Weekly Review", "Data Health", "AI Summary"]
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

# --------------------------------------------------------------------------
# Materiality thresholds for the banner.
#
# A page-tier check firing is always worth listing, but it is not always worth
# interrupting someone. Revenue under review goes red only once it clears
# EITHER gate - the same two-gate shape as the reconciliation check itself
# (DECISIONS.md part 2): a relative test alone would scream on a quiet week,
# an absolute test alone would sleep through a big one.
#
# Data Health lists every item regardless of materiality.
# --------------------------------------------------------------------------
# Imported from app/claims.py, which is the single definition - the
# required-content check needs the same numbers and two copies would drift.
#
# The absolute gate was first set at $1,000. Applied to the chart markers it
# turned 5 of the last 13 weeks red - three of them for ~$1,100 against a
# ~$325,000 week, which is 0.33% and does not visibly move the trend line.
# A gate that fires on a third of a percent is the "red for $58" problem one
# order of magnitude up.
#
# $5,000 is calibrated against the scale of a week here (~$330k): roughly 1.5%
# of a week, and an amount worth a person's time to investigate. It leaves
# every previously reviewed banner outcome unchanged - 2026-11-16 amber,
# 2026-09-28 red, 2026-10-05 red, 2026-09-14 green - while reducing the red
# markers to the two weeks that actually distort the chart.

#: Default span of the Inspect chart, and how many days of context to keep
#: after the flagged day so it is not pinned to the right edge.
INSPECT_WEEKS = 13
INSPECT_TRAILING_DAYS = 7

#: Above this actual-to-expected ratio the Inspect chart defaults to log scale,
#: because a linear axis compresses the expected band into a flat line.
LOG_SCALE_RATIO = 10

#: Rows per direction in Top movers. Each direction is ranked separately, so
#: both lists are this long whenever enough accounts moved that way.
TOP_MOVERS_N = 8

BANNER_COPY = {
    "red": "Action needed",
    "amber": "Minor item under review",
    "green": "All clear",
}


def banner_state(week_start, week_revenue: float | None) -> tuple[str, str, str]:
    """Return (status, headline, detail) for the data-health banner.

    Red is reserved for revenue under review that clears a materiality gate.
    Amber covers everything else that wants a human: an immaterial amount under
    review, a late-arriving feed, or items in the statistical review queue.

    Late arrivals and review-queue items cannot turn the banner red, because
    neither distorts the revenue figure - but they do not leave it green
    either. A stalled feed needs an owner even when this week's dollars happen
    to tie out.

    The returned detail is rendered by health_banner(), which is raw HTML, so
    it uses the PLAIN money helpers. The markdown-escaped md_money* helpers
    belong in st.markdown/st.caption strings only - putting a backslash-escaped
    dollar into HTML prints the backslash.
    """
    ur = q.under_review(week_start)
    health = q.health_status(week_start)
    amount = ur["amount"]
    share = (amount / week_revenue * 100) if week_revenue else 0.0

    material = amount > MATERIAL_ABS_USD or share > MATERIAL_SHARE_PCT
    if material:
        status = "red"
        detail = (
            f"{money_cents(amount)} under review — {share_pct(share)} of the week, "
            f"across {plural(ur['account_days'], 'account-day')}. "
            f"Over the {MATERIAL_SHARE_PCT}% / {money(MATERIAL_ABS_USD)} "
            "materiality threshold."
        )
    elif amount > 0:
        status = "amber"
        detail = (
            f"{money_cents(amount)} under review — {share_pct(share)} of the week, "
            f"across {plural(ur['account_days'], 'account-day')}. "
            f"Below the {MATERIAL_SHARE_PCT}% / {money(MATERIAL_ABS_USD)} "
            "materiality threshold, so listed but not escalated."
        )
    elif health["n_late"] or health["n_review"]:
        status = "amber"
        parts = []
        if health["n_late"]:
            parts.append(f"{plural(health['n_late'], 'late-arriving account-day')}")
        if health["n_review"]:
            parts.append(f"{plural(health['n_review'], 'review-queue item')}")
        detail = (
            f"No revenue under review. {' and '.join(parts)} — neither distorts the "
            "revenue figure, but both want a look."
        )
    else:
        status = "green"
        detail = "Nothing under review, no late arrivals, review queue empty."

    return status, BANNER_COPY[status], detail


# --------------------------------------------------------------------------
# Page 1 - Weekly Review
# --------------------------------------------------------------------------

def page_weekly_review(week_start) -> None:
    st.title("Weekly Review")

    kpis_for_banner = q.week_kpis(week_start)
    status, headline, detail = banner_state(
        week_start, kpis_for_banner.get("revenue") if kpis_for_banner else None
    )
    banner_col, button_col = st.columns([5, 1])
    with banner_col:
        health_banner(status, f"Data health — {headline}", detail)
    with button_col:
        if st.button("Open Data Health", use_container_width=True):
            st.session_state.page = "Data Health"
            st.session_state.health_week = week_start
            st.rerun()

    kpis = q.week_kpis(week_start)
    if not kpis:
        md_warning(f"No weekly data for {week_start}.")
        return

    if kpis["is_partial"]:
        md_warning(
            f"**Partial week.** {kpis['reason']} Revenue and account count below are "
            f"real but cover {kpis['days_present']} of {q.DAYS_IN_WEEK} days, so they "
            "are not comparable with a full week.",
            icon="⚠️",
        )

    mode_label = st.radio(
        "Revenue basis",
        ["Reported", "Excluding items under review"],
        horizontal=True,
        help="Reported is the figure as landed. Excluding items under review drops "
             "account-days carrying an open amount-affecting data-quality issue.",
    )
    mode = "reported" if mode_label == "Reported" else "adjusted"
    md_caption("A conservative floor, not a corrected figure.")

    _kpi_row(week_start, kpis)
    _under_review_line(week_start, kpis, mode)

    st.markdown("### Revenue trend, last 13 complete weeks")
    markers = q.week_issue_markers(week_start) if mode == "reported" else None
    by_product = q.revenue_13w(week_start, "PRODUCT", mode)
    by_segment = q.revenue_13w(week_start, "SEGMENT", mode)
    left, right = st.columns(2)
    with left:
        st.caption("By product")
        _trend_chart(by_product, markers)
    with right:
        st.caption("By segment")
        _trend_chart(by_segment, markers)

    if mode == "adjusted":
        removed = q.flagged_account_days()
        md_caption(
            f"**Excluding items under review — a conservative floor, not a corrected "
            f"figure.** {plural(len(removed), 'account-day')} carrying an open "
            "reconciliation, unit-error or duplicate-load issue have been dropped "
            "from these charts, across all weeks shown. The whole account-day is "
            "removed, not just the excess. Late arrivals are *not* dropped — their "
            "values are correct and only the load time moved."
        )
    else:
        md_caption("**Reported** — figures as landed, including amounts still under "
                   "review. Weeks carrying an open page-tier issue are marked on the "
                   "charts; hover a marker to see which issue.")

    st.markdown("### Top movers, week over week")
    if kpis["is_partial"]:
        note("Withheld for a partial week: the comparison would be against a full "
             "seven-day week.")
        return

    # Movers are ALWAYS computed excluding account-days under review, whatever
    # the Reported/Excluding toggle says. The toggle governs the revenue
    # figures; this list answers a different question - what changed in the
    # business - and a data-quality artefact is not business movement. On the
    # reported basis the two largest movers for 2026-10-05 were the unit error
    # itself (+$133,750) and the same account's rebound the following week
    # (-$13,992), which pushed two real movers off the list entirely.
    # DECISIONS.md part 11. The brief uses the same basis, so the app and the
    # summary cannot disagree about who moved.
    movers = q.top_movers(week_start, limit=TOP_MOVERS_N,
                          basis="excluding_under_review")
    if movers.empty:
        note("No prior complete week to compare against.")
        return
    up = movers[movers["DIRECTION"] == "up"]
    down = movers[movers["DIRECTION"] == "down"]
    totals = q.mover_counts(week_start)
    left, right = st.columns(2)
    with left:
        st.caption(f"Up — top {len(up)} of {totals['up']:,} accounts that rose")
        _movers_table(up)
    with right:
        st.caption(f"Down — top {len(down)} of {totals['down']:,} accounts that fell")
        _movers_table(down)

    md_caption("Movers exclude account-days under review; see Data Health for "
               "those items.")

    if len(up) != len(down):
        note(f"Lists are uneven because only {min(len(up), len(down))} account(s) "
             f"moved in one direction, not because of where the list was cut.")

    _excluded_movers_line(week_start, mode)

    note(f"Each side shows the top {TOP_MOVERS_N} by dollar change, ranked "
         "separately so the two lists are the same length. Dollar change rather "
         "than percentage: a large percentage move on a very small account is not "
         "a mover worth a review meeting.")


def _excluded_movers_line(week_start, mode: str) -> None:
    """In Reported mode, say what the basis difference does to this list.

    The revenue figures above are on the reported basis while this list is not,
    so the gap is stated rather than left for a reader to notice.

    Absence is not the only effect, and on its own it understates the case. For
    2026-10-05 the unit-error account does not disappear - it moves from the
    largest riser at +$133,750 to a $286 faller. Reporting only "1 account is
    absent" would hide the larger change, so a mover whose figure shifts by
    more than the materiality threshold is named too.
    """
    if mode != "reported":
        return
    reported = q.top_movers(week_start, limit=TOP_MOVERS_N, basis="reported")
    adjusted = q.top_movers(week_start, limit=TOP_MOVERS_N,
                            basis="excluding_under_review")
    if reported.empty:
        return

    adjusted_by_account = dict(
        zip(adjusted["ACCOUNT_ID"], adjusted["CHANGE_USD"])
    )
    changes: list[str] = []
    for row in reported.itertuples():
        before = float(row.CHANGE_USD)
        if row.ACCOUNT_ID not in adjusted_by_account:
            changes.append(f"{row.ACCOUNT_ID} ({signed_money(before)} → not in list)")
            continue
        after = float(adjusted_by_account[row.ACCOUNT_ID])
        if abs(before - after) > MATERIAL_ABS_USD:
            changes.append(
                f"{row.ACCOUNT_ID} ({signed_money(before)} → {signed_money(after)})"
            )

    if not changes:
        return

    md(f"**{plural(len(changes), 'account')} would rank differently on a "
       f"reported basis** — {', '.join(changes)}. That movement comes from "
       "account-days under review, not from the business.")
    if st.button("See these in Data Health", key="movers_to_health"):
        st.session_state.page = "Data Health"
        st.session_state.health_week = week_start
        st.rerun()


def _kpi_row(week_start, kpis: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)

    # When a material amount of the week is under review, the week-over-week
    # and trailing comparisons are arithmetically right but not yet a business
    # signal: part of what moved is a known data-quality fault. They render
    # grey rather than green/red, because a coloured arrow asserts a direction
    # the data does not support until the review clears.
    ur = q.under_review(week_start)
    share = (ur["amount"] / kpis["revenue"] * 100) if kpis["revenue"] else 0.0
    material = ur["amount"] > MATERIAL_ABS_USD or share > MATERIAL_SHARE_PCT
    caveat = (f"includes {money(ur['amount'])} under review" if material else None)

    with c1:
        suffix = (f" ({kpis['days_present']} of {q.DAYS_IN_WEEK} days)"
                  if kpis["is_partial"] else "")
        render_metric_card("Revenue this week", money(kpis["revenue"]),
                           help_text=f"Week beginning {week_start}{suffix}")
    withheld = "Withheld — partial week"
    with c2:
        render_metric_card(
            "Week over week",
            pct(kpis["wow_pct"]) if kpis["wow_pct"] is not None else "—",
            delta=(f"from {money(kpis['prior_revenue'])}"
                   if kpis["prior_revenue"] is not None else None),
            direction="neutral" if material else direction_of(kpis["wow_pct"]),
            help_text=(caveat if material
                       else (f"vs {kpis['prior_week']}" if kpis["prior_week"]
                             else withheld)),
        )
    with c3:
        render_metric_card(
            "vs 4-week average",
            pct(kpis["vs_4w_pct"]) if kpis["vs_4w_pct"] is not None else "—",
            delta=(f"avg {money(kpis['trailing_4w'])}"
                   if kpis["trailing_4w"] is not None else None),
            direction="neutral" if material else direction_of(kpis["vs_4w_pct"]),
            help_text=(caveat if material
                       else ("Trailing 4 complete weeks, this week excluded"
                             if kpis["trailing_4w"] is not None else withheld)),
        )
    with c4:
        render_metric_card("Active accounts", f"{kpis['active_accounts']:,}",
                           help_text="Billed at least once this week")

    if material:
        md_caption(
            f"Week-over-week and trailing comparisons are shown in grey: "
            f"{md_money(ur['amount'])} of this week ({share_pct(share)}) is under "
            "review, so the direction is not yet a business signal. Switch the "
            "basis above to see the figure excluding those items."
        )


def _under_review_line(week_start, kpis: dict, mode: str) -> None:
    ur = q.under_review(week_start)
    if not ur["amount"]:
        return
    share = ur["amount"] / kpis["revenue"] * 100 if kpis["revenue"] else 0.0
    where = plural(ur["account_days"], "account-day")
    material = ur["amount"] > MATERIAL_ABS_USD or share > MATERIAL_SHARE_PCT
    flag = "" if material else " Below the materiality threshold."
    if mode == "reported":
        md(
            f"**{md_money_cents(ur['amount'])} of this week's revenue is under "
            f"review** ({share_pct(share)} of {md_money(kpis['revenue'])}), across "
            f"{where} — {ur['issues']}.{flag}"
        )
    else:
        md(
            f"**{md_money_cents(ur['amount'])} excluded from this week** "
            f"({share_pct(share)} of the reported {md_money(kpis['revenue'])}), "
            f"across {where} — {ur['issues']}.{flag}"
        )


WEEK_FMT = "%b %d"


def _with_week_label(df: pd.DataFrame, column: str = "WEEK_START") -> pd.DataFrame:
    """Add a 'Sep 01' label used as an ordinal x axis.

    A temporal axis picked its own tick positions and labelled them with
    weekdays - "Tue 25", "Sat 03" - which never line up with Monday week
    starts and invite the reader to think the points are daily. Weeks are
    evenly spaced and every one is present, so an ordinal axis is both honest
    and guarantees exactly one tick per week.
    """
    out = df.copy()
    out["WEEK_LABEL"] = pd.to_datetime(out[column]).dt.strftime(WEEK_FMT)
    return out


def _week_order(df: pd.DataFrame, column: str = "WEEK_START") -> list[str]:
    weeks = sorted(pd.to_datetime(df[column]).dt.normalize().unique())
    return [pd.Timestamp(w).strftime(WEEK_FMT) for w in weeks]


def classify_markers(markers: pd.DataFrame | None) -> pd.DataFrame | None:
    """Split weeks with open items into material and minor.

    Same rule as the banner (DECISIONS.md part 9). Without it, 10 of the last
    13 weeks carried a red marker, which conveys exactly as much as no markers
    at all.
    """
    if markers is None or markers.empty:
        return markers
    out = markers.copy()
    share = out["SHARE_PCT"].fillna(0.0)
    amount = out["REVENUE_AT_RISK"].fillna(0.0)
    out["IS_MATERIAL"] = (amount > MATERIAL_ABS_USD) | (share > MATERIAL_SHARE_PCT)
    out["AT_RISK_LABEL"] = amount.map(money_cents)
    out["SHARE_LABEL"] = share.map(share_pct)
    return out


def _trend_chart(df: pd.DataFrame, markers: pd.DataFrame | None = None) -> None:
    if df.empty:
        note("No data for this window.")
        return

    plotted = _with_week_label(df)
    order = _week_order(df)

    line = (
        alt.Chart(plotted)
        .mark_line(point=True)
        .encode(
            x=alt.X("WEEK_LABEL:O", sort=order, title=None,
                    axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("REVENUE:Q", title="Revenue", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("DIMENSION:N", title=None,
                            legend=alt.Legend(orient="bottom")),
            tooltip=[
                alt.Tooltip("WEEK_LABEL:N", title="Week beginning"),
                alt.Tooltip("DIMENSION:N", title=""),
                alt.Tooltip("REVENUE:Q", title="Revenue", format="$,.0f"),
            ],
        )
    )
    layers = [line]

    marks = classify_markers(markers)
    if marks is not None and not marks.empty:
        shown = _with_week_label(
            marks[marks["WEEK_START"].isin(set(df["WEEK_START"]))]
        )
        tips = [
            alt.Tooltip("WEEK_LABEL:N", title="Week beginning"),
            alt.Tooltip("ISSUES:N", title="Open issue"),
            alt.Tooltip("ACCOUNT_DAYS:Q", title="Account-days"),
            alt.Tooltip("AT_RISK_LABEL:N", title="Under review"),
            alt.Tooltip("SHARE_LABEL:N", title="Share of week"),
        ]
        material = shown[shown["IS_MATERIAL"]]
        minor = shown[~shown["IS_MATERIAL"]]

        if not material.empty:
            layers.append(
                alt.Chart(material)
                .mark_rule(color="#dc3545", strokeDash=[3, 3], opacity=0.55)
                .encode(x=alt.X("WEEK_LABEL:O", sort=order), tooltip=tips)
            )
            layers.append(
                alt.Chart(material)
                .mark_point(shape="triangle-down", size=110, filled=True,
                            color="#dc3545", yOffset=-8)
                .encode(x=alt.X("WEEK_LABEL:O", sort=order),
                        y=alt.value(0), tooltip=tips)
            )
        if not minor.empty:
            # Still discoverable on hover, but it does not shout.
            layers.append(
                alt.Chart(minor)
                .mark_point(size=34, filled=True, color="#9aa0a6", opacity=0.75,
                            yOffset=-8)
                .encode(x=alt.X("WEEK_LABEL:O", sort=order),
                        y=alt.value(0), tooltip=tips)
            )

    st.altair_chart(alt.layer(*layers).properties(height=280),
                    use_container_width=True)


def _movers_table(df: pd.DataFrame) -> None:
    if df.empty:
        note("None.")
        return
    shown = df[["ACCOUNT_ID", "SEGMENT", "REVENUE", "CHANGE_USD", "CHANGE_PCT"]].copy()
    shown.columns = ["Account", "Segment", "Revenue", "Change ($)", "Change (%)"]
    st.dataframe(
        styled_table(
            shown,
            {"Revenue": plain_money,
             "Change ($)": signed_money,
             "Change (%)": signed_pct},
            color_columns=["Change ($)", "Change (%)"],
        ),
        hide_index=True, use_container_width=True,
    )


# --------------------------------------------------------------------------
# Page 2 - Data Health
# --------------------------------------------------------------------------

def page_data_health(week_start) -> None:
    st.title("Data Health")

    kpis = q.week_kpis(week_start)
    status, headline, detail = banner_state(
        week_start, kpis.get("revenue") if kpis else None
    )
    health_banner(status, f"Week of {week_start} — {headline}", detail)
    note("Every page-tier item is listed below regardless of materiality. The banner "
         "says whether to escalate; the tables say what happened.")

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
            # Reconciliation compares a whole account-day against the ledger, so
            # it has no single product. NULL rendered as the string "None",
            # which reads like a missing value rather than a scope.
            shown["PRODUCT"] = shown["PRODUCT"].fillna("All products")
            shown.columns = ["Date", "Account", "Product", "Check", "Detail",
                             "Usage $", "Ledger $", "Impact $"]
            st.dataframe(
                styled_table(shown, {
                    "Usage $": plain_money_cents,
                    "Ledger $": plain_money_cents,
                    "Impact $": plain_money_cents,
                }),
                hide_index=True, use_container_width=True,
            )
        if not late.empty:
            st.caption("Late arrivals — values correct, freshness fault")
            shown = late[["USAGE_DATE", "ACCOUNT_ID", "LATE_ROW_COUNT", "LAG_DAYS",
                          "REVENUE_AFFECTED"]].copy()
            shown.columns = ["Date", "Account", "Late rows", "Lag (days)", "Revenue $"]
            st.dataframe(
                styled_table(shown, {
                    "Lag (days)": lambda v: num(v, 1),
                    "Revenue $": plain_money_cents,
                }),
                hide_index=True, use_container_width=True,
            )

    md(f"### Review queue — z-score |z| > {q.REVIEW_Z:g}")
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
        st.dataframe(
            styled_table(shown, {
                "Actual $": plain_money_cents,
                "Expected $": plain_money_cents,
                "z": lambda v: num(v, 2),
            }, color_columns=["z"]),
            hide_index=True, use_container_width=True,
        )

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

    full_year = st.checkbox(
        "Show full year", value=False,
        help=f"Off by default: the chart shows the {INSPECT_WEEKS} weeks up to "
             "the flagged day, where daily movement is legible. A full year of "
             "daily points compresses the band until the flagged day is hard to "
             "place.",
    )
    windowed = series if full_year else _recent_window(series, flagged_on)

    # A 1000x unit error flattens the expected band to a line at the bottom of
    # a linear axis - the band is the whole point of the chart, so log scale is
    # the default once the flagged value dwarfs the expectation.
    ratio = _flagged_ratio(windowed, flagged_on)
    log_default = ratio is not None and ratio > LOG_SCALE_RATIO
    log_scale = st.checkbox(
        "Log scale", value=log_default,
        help=(f"On by default here: the flagged value is {ratio:.0f}x its expected "
              f"level, which flattens the band on a linear axis."
              if log_default else
              "Useful when a spike compresses the expected band out of view."),
    )

    span = (f"full series, {len(series)} days" if full_year
            else f"{INSPECT_WEEKS} weeks to {flagged_on}, {len(windowed)} days")
    md_caption(f"{account_id} · {product} — actual against the expected band "
               f"(±2σ, asymmetric because the model is multiplicative). {span}"
               + (", log scale." if log_scale else "."))
    _series_chart(windowed, flagged_on, log_scale)


def _flagged_ratio(series: pd.DataFrame, flagged_on) -> float | None:
    """How many times its expected level the flagged day actually reached."""
    row = series[pd.to_datetime(series["USAGE_DATE"]) == pd.to_datetime(flagged_on)]
    if row.empty:
        return None
    actual = float(row["VALUE"].iloc[0])
    expected = float(row["EXPECTED_VALUE"].iloc[0])
    if expected <= 0:
        return None
    return actual / expected


def _recent_window(series: pd.DataFrame, flagged_on) -> pd.DataFrame:
    """The INSPECT_WEEKS weeks ending just after the flagged day.

    Anchored on the flagged day rather than on the end of the series, so the
    point under investigation is always in view - an item flagged in March
    would otherwise fall outside a window pinned to the latest data.
    """
    dates = pd.to_datetime(series["USAGE_DATE"])
    anchor = pd.to_datetime(flagged_on)
    start = anchor - pd.Timedelta(weeks=INSPECT_WEEKS)
    end = anchor + pd.Timedelta(days=INSPECT_TRAILING_DAYS)
    window = series[(dates >= start) & (dates <= end)]
    return window if not window.empty else series


#: Explicit date format on the daily axis. Altair's default multi-format emits
#: weekday labels ("Tue 25", "Sat 03"), which read as though the series were
#: keyed on weekday rather than date.
DAY_AXIS = alt.Axis(format="%b %d", tickCount=8, labelAngle=-45)


def _series_chart(df: pd.DataFrame, flagged_on, log_scale: bool = False) -> None:
    # symlog rather than log: a pipeline gap is a genuine zero and a plain log
    # scale drops it silently. symlog is linear near zero and logarithmic
    # beyond, so a gap day still plots.
    y_scale = alt.Scale(type="symlog") if log_scale else alt.Scale(type="linear")
    y_axis = alt.Axis(format="$,.0f")

    band = (
        alt.Chart(df)
        .mark_area(opacity=0.18)
        .encode(
            x=alt.X("USAGE_DATE:T", title=None, axis=DAY_AXIS),
            y=alt.Y("BAND_LOW:Q", title="Revenue", axis=y_axis, scale=y_scale),
            y2="BAND_HIGH:Q",
        )
    )
    expected = (
        alt.Chart(df).mark_line(strokeDash=[4, 3])
        .encode(x=alt.X("USAGE_DATE:T", axis=DAY_AXIS),
                y=alt.Y("EXPECTED_VALUE:Q", scale=y_scale, axis=y_axis))
    )
    actual = (
        alt.Chart(df).mark_line()
        .encode(
            x=alt.X("USAGE_DATE:T", axis=DAY_AXIS),
            y=alt.Y("VALUE:Q", scale=y_scale, axis=y_axis),
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
            .encode(x=alt.X("USAGE_DATE:T", axis=DAY_AXIS),
                    y=alt.Y("VALUE:Q", scale=y_scale, axis=y_axis))
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
    st.dataframe(
        styled_table(shown, {
            "Threshold": lambda v: "—" if pd.isna(v) else f"{v:g}",
            "TP": as_int,
            "FP": as_int,
            "FN": as_int,
            "Precision": lambda v: num(v, 3),
            "Recall": lambda v: num(v, 3),
            "F1": lambda v: num(v, 3),
            "Chance": lambda v: num(v, 2),
        }),
        hide_index=True, use_container_width=True,
    )

    with st.expander("Recall by anomaly type"):
        per_type = q.scorecard(scenario, overall=False)
        if per_type.empty:
            note("No per-type rows.")
        else:
            # One flat, readable column label. A ["DETECTOR", "THRESHOLD"]
            # MultiIndex put NaN in the header for every detector with no
            # threshold, and Styler.format matches columns by label - NaN never
            # equals NaN, so those columns silently rendered unformatted
            # (0.952381). Collapsing the label avoids the NaN key entirely and
            # reads better than "('reconciliation', nan)".
            labelled = per_type.copy()
            labelled["COLUMN"] = [
                d if pd.isna(t) else f"{d} · |z|>{t:g}"
                for d, t in zip(labelled["DETECTOR"], labelled["THRESHOLD"])
            ]
            pivot = labelled.pivot_table(
                index="ANOMALY_TYPE", columns="COLUMN",
                values="RECALL", aggfunc="max", dropna=False,
            )
            st.dataframe(
                pivot.style.format(lambda v: num(v, 2), na_rep="—"),
                use_container_width=True,
            )
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

    record = q.ai_summary(week_start)
    if not record:
        status_badge("grey", "No summary for this week")
        md(
            f"""
No summary is stored for the week beginning **{week_start}**. Generate one:

```bash
python scripts/generate_summary.py {week_start}                     # Anthropic API
python scripts/generate_summary.py {week_start} --backend template  # no API key
SUMMARY_BACKEND=cortex python scripts/generate_summary.py {week_start}
```

The Anthropic backend needs `ANTHROPIC_API_KEY` in `.env`. The `cortex` backend
runs `AI_COMPLETE` inside Snowflake and needs a non-trial account. `template`
fills a fixed sentence per bullet from the same aggregates and needs neither —
useful for exercising this page, though its numbers come straight from the JSON
so it passes verification by construction.
            """
        )
        return

    result = verify(record["SUMMARY"], record["INPUT_JSON"])
    claim_issues = check_claims(record["SUMMARY"], record["INPUT_JSON"])
    claim_errors = [c for c in claim_issues if c.severity == "error"]
    claim_warnings = [c for c in claim_issues if c.severity != "error"]

    # Numeric verification is necessary but not sufficient: a true number can
    # carry a false claim (DECISIONS.md part 11). Both have to pass.
    ready = result.ok and not claim_errors

    if ready:
        extra = []
        if result.low_confidence_numbers:
            extra.append(
                f"{result.low_confidence_numbers} figure(s) matched a value but "
                "no field whose name fits the surrounding words - review those."
            )
        if claim_warnings:
            extra.append(f"{len(claim_warnings)} advisory warning(s).")
        health_banner(
            "green", "Ready to send",
            f"All {result.total_numbers} figures and {result.total_identifiers} "
            "identifiers appear in the source aggregates, and no claim check "
            "failed." + ("  " + "  ".join(extra) if extra else ""),
        )
    elif not result.ok:
        failing = sum(1 for b in result.bullets if not b.ok)
        health_banner(
            "red", "Not ready to send — figures do not match",
            f"{failing} of {len(result.bullets)} bullets contain a value that is not "
            f"in the source aggregates "
            f"({result.matched_numbers}/{result.total_numbers} figures matched, "
            f"{result.matched_identifiers}/{result.total_identifiers} identifiers).",
        )
    else:
        health_banner(
            "red", "Not ready to send — a claim is unsupported",
            f"Every figure matches the source aggregates, but "
            f"{len(claim_errors)} claim check(s) failed. A correct number can "
            "still carry a false statement.",
        )

    md_caption(
        f"Week beginning {record['WEEK_START']} · model `{record['MODEL']}` · "
        f"prompt hash `{record['PROMPT_HASH'][:12]}` · generated {record['CREATED_AT']} UTC"
    )

    st.markdown("### Summary")
    for bullet in result.bullets:
        _verified_bullet(bullet)

    if claim_issues:
        st.markdown("### Claim checks")
        md_caption(
            "What the numbers alone cannot test: whether a direction word matches "
            "the sign, whether a named account holds the role the text gives it, "
            "whether a move the text explains commercially was actually a "
            "data-quality rebound, and whether a superlative or a claim that "
            "movement is real goes beyond the aggregates."
        )
        for issue in claim_issues:
            icon = "🚩" if issue.severity == "error" else "⚠️"
            md(f"{icon} **{issue.check}** (bullet {issue.bullet_index}) — {issue.detail}")
            md_caption(f"in: {issue.quote[:200]}")
    else:
        status_badge("green", "All claim checks passed")

    st.markdown("### Verification detail")
    md_caption(
        "Every figure is matched against the stored aggregates within the precision "
        "it was written to — a value given as $368.1K is held to the hundred, one "
        "given to the cent is held to the cent. Sign is checked too, and a movement "
        "word counts as a sign. Small integers pick their field from the words around "
        "them, and one with no field that fits reads 'low confidence' rather than "
        "'yes'. Account identifiers are checked as strings; dates, unit modifiers "
        "like '4-week', and the digits inside an identifier are not quantities."
    )
    rows = []
    for i, bullet in enumerate(result.bullets, start=1):
        for check in bullet.numbers:
            sign = {1: "+", -1: "−", None: "—"}[check.asserted_sign]
            rows.append({"Bullet": i, "Value": check.text, "Kind": "figure",
                         "Sign asserted": sign,
                         "Matched": {"matched": "yes",
                                     "low_confidence": "low confidence",
                                     "unmatched": "NO"}[check.confidence],
                         "Source field": check.matched_against or "—"})
        for check in bullet.identifiers:
            rows.append({"Bullet": i, "Value": check.text, "Kind": "identifier",
                         "Sign asserted": "—",
                         "Matched": "yes" if check.matched else "NO",
                         "Source field": check.matched_against or "—"})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    with st.expander("Input aggregates (what the model was given)"):
        md_caption(
            "Aggregates only — no event rows, and no account detail beyond the named "
            "movers. This is the exact JSON the summary is verified against."
        )
        try:
            st.json(json.loads(record["INPUT_JSON"]))
        except (ValueError, TypeError):
            st.code(record["INPUT_JSON"], language="json")


def _verified_bullet(bullet) -> None:
    """One bullet with a pass/fail marker and any unmatched values named."""
    if not bullet.ok:
        mark = "🚩"
    elif bullet.low_confidence:
        mark = "⚠️"
    else:
        mark = "✅"
    text = bullet.text.lstrip("-").strip()
    md(f"{mark} {text}")
    if not bullet.ok:
        md_caption(
            "Not found in the source aggregates: "
            + ", ".join(f"`{v}`" for v in bullet.unmatched)
        )
    elif bullet.low_confidence:
        md_caption(
            "Present in the aggregates but no field fits the wording, so the "
            "figure is unconfirmed rather than verified: "
            + ", ".join(f"`{v}`" for v in bullet.low_confidence)
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
        st.error("No weeks found in MARTS.FCT_DAILY_REVENUE. Has dbt been run?")
        return

    # Default to the newest COMPLETE week. The data window ends mid-week, so the
    # newest week can hold a single day; defaulting to it showed a -84% WoW that
    # was an artefact of one day against seven.
    fallback = q.default_week()
    chosen = st.session_state.get("health_week", fallback)
    index = available.index(chosen) if chosen in available else available.index(fallback)
    week_start = st.sidebar.selectbox(
        "Week beginning", available, index=index, format_func=q.week_label
    )
    st.session_state.health_week = week_start

    if q.is_partial(week_start):
        st.sidebar.warning(
            f"Partial week — {q.week_days()[week_start]} of {q.DAYS_IN_WEEK} days. "
            "Comparisons are withheld.",
            icon="⚠️",
        )

    n_complete = len(q.complete_weeks())
    st.sidebar.caption(
        f"{len(available)} weeks · {n_complete} complete · default is the newest "
        "complete week. The warehouse auto-suspends after 60s, so the first query "
        "of a session is slow."
    )

    if st.session_state.page == "Weekly Review":
        page_weekly_review(week_start)
    elif st.session_state.page == "Data Health":
        page_data_health(week_start)
    else:
        page_ai_summary(week_start)


main()
