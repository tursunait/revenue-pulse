"""Reusable Streamlit UI components.

Follows the pattern from the BNY compliance app (streamlit_app/components):
each component emits a div/span carrying a CSS class via
st.markdown(unsafe_allow_html=True), and the classes live in one stylesheet
injected once per run. No st.metric, so the cards can carry a signed delta and
a help line that st.metric does not support.

Two deliberate changes from the original:

* The CSS is a Python constant rather than a file read with
  Path(__file__).parents[1] / "styles". A Streamlit in Snowflake app is served
  from a stage, and keeping the stylesheet inline removes any dependence on
  file layout there.
* Every interpolated value is escaped with html.escape(). The originals
  interpolate raw, which is fine for literal labels; here the labels include
  account ids and product names read from Snowflake, so they are escaped as a
  matter of course.
"""

from __future__ import annotations

import html

import streamlit as st

STYLES = """
<style>
:root {
  --rp-ink:        #14293d;
  --rp-accent:     #0077c8;
  --rp-teal:       #00a3b5;
  --rp-gray-dark:  #4a4a4a;
  --rp-gray-mid:   #767676;
  --rp-gray-light: #d3d3d3;
  --rp-surface:    #ffffff;
  --rp-danger:     #dc3545;
  --rp-warning:    #ffc107;
  --rp-success:    #28a745;
}

.metric-card {
  background: var(--rp-surface);
  border-radius: 10px;
  padding: .9rem 1rem;
  box-shadow: 0 1px 3px rgba(0,0,0,.12);
  border-left: 4px solid var(--rp-teal);
  height: 100%;
}
.metric-title { color: var(--rp-gray-dark); font-size: .86rem; }
.metric-value { color: var(--rp-ink); font-size: 1.8rem; font-weight: 700; line-height: 1.2; }
.metric-delta { font-size: .8rem; font-weight: 600; }
.metric-delta.up   { color: var(--rp-success); }
.metric-delta.down { color: var(--rp-danger); }
.metric-delta.flat { color: var(--rp-accent); }
.metric-help { color: var(--rp-gray-mid); font-size: .78rem; margin-top: .2rem; }

.status-badge {
  display: inline-flex;
  align-items: center;
  border-radius: 9999px;
  padding: .22rem .66rem;
  font-size: .75rem;
  font-weight: 700;
  margin-right: .3rem;
}
.status-badge.red   { background: var(--rp-danger);  color: #fff; }
.status-badge.amber { background: var(--rp-warning); color: #6b5500; }
.status-badge.green { background: var(--rp-success); color: #fff; }
.status-badge.grey  { background: #e9ecef; color: #495057; }

.health-banner {
  border-radius: 10px;
  padding: .8rem 1rem;
  margin-bottom: .4rem;
  border-left: 5px solid var(--rp-gray-light);
  background: var(--rp-surface);
}
.health-banner.red   { border-left-color: var(--rp-danger);  background: #fdecee; }
.health-banner.amber { border-left-color: var(--rp-warning); background: #fff8e1; }
.health-banner.green { border-left-color: var(--rp-success); background: #eaf7ee; }
.health-headline { font-weight: 700; color: var(--rp-ink); font-size: 1rem; }
.health-detail   { color: var(--rp-gray-dark); font-size: .84rem; margin-top: .15rem; }

.note { color: var(--rp-gray-mid); font-size: .8rem; }
</style>
"""


def load_styles() -> None:
    """Inject the stylesheet. Call once, at the top of the app."""
    st.markdown(STYLES, unsafe_allow_html=True)


def render_metric_card(
    title: str,
    value: str,
    delta: str | None = None,
    help_text: str | None = None,
    direction: str = "flat",
) -> None:
    """A KPI card. `value` is pre-formatted by the caller, as in the original.

    `direction` is "up" | "down" | "flat" and only colours the delta. The BNY
    stylesheet renders every delta in blue; a revenue review needs the sign to
    read at a glance, so the three variants were added here.
    """
    delta_html = (
        f'<div class="metric-delta {html.escape(direction)}">{html.escape(delta)}</div>'
        if delta else ""
    )
    help_html = f'<div class="metric-help">{html.escape(help_text)}</div>' if help_text else ""
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-title">{html.escape(title)}</div>
          <div class="metric-value">{html.escape(value)}</div>
          {delta_html}
          {help_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def status_badge(status: str, label: str | None = None) -> None:
    """A pill. `status` is red | amber | green | grey."""
    normalized = (status or "grey").lower().replace("_", "-")
    text = label or normalized.title()
    st.markdown(
        f'<span class="status-badge {html.escape(normalized)}">{html.escape(text)}</span>',
        unsafe_allow_html=True,
    )


def health_banner(status: str, headline: str, detail: str = "") -> None:
    """The data-health banner at the top of the Weekly Review."""
    normalized = (status or "grey").lower()
    detail_html = f'<div class="health-detail">{html.escape(detail)}</div>' if detail else ""
    st.markdown(
        f"""
        <div class="health-banner {html.escape(normalized)}">
          <div class="health-headline">{html.escape(headline)}</div>
          {detail_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def note(text: str) -> None:
    st.markdown(f'<div class="note">{html.escape(text)}</div>', unsafe_allow_html=True)


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.0f}"


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def direction_of(value: float | None) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else ("down" if value < 0 else "flat")
