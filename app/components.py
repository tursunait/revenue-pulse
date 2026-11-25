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
import re

import pandas as pd
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


def _h(text: object) -> str:
    """Escape text for the raw-HTML components.

    html.escape() leaves "$" alone, and Streamlit's markdown pass still scans
    the string it is handed - even one containing HTML - for a PAIR of dollar
    signs to treat as LaTeX. A card showing a single amount survives by luck;
    one showing two (a value and a dollar delta) would pair up and render as
    maths. Emitting &#36; removes the possibility: HTML renders it as "$" and
    the markdown pass sees no delimiter.
    """
    return html.escape(str(text)).replace("$", "&#36;")


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
        f'<div class="metric-delta {_h(direction)}">{_h(delta)}</div>'
        if delta else ""
    )
    help_html = f'<div class="metric-help">{_h(help_text)}</div>' if help_text else ""
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-title">{_h(title)}</div>
          <div class="metric-value">{_h(value)}</div>
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
        f'<span class="status-badge {_h(normalized)}">{_h(text)}</span>',
        unsafe_allow_html=True,
    )


def health_banner(status: str, headline: str, detail: str = "") -> None:
    """The data-health banner at the top of the Weekly Review."""
    normalized = (status or "grey").lower()
    detail_html = f'<div class="health-detail">{_h(detail)}</div>' if detail else ""
    st.markdown(
        f"""
        <div class="health-banner {_h(normalized)}">
          <div class="health-headline">{_h(headline)}</div>
          {detail_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def note(text: str) -> None:
    st.markdown(f'<div class="note">{_h(text)}</div>', unsafe_allow_html=True)


def money(value: float | None) -> str:
    """Plain dollar string. Safe inside the raw-HTML components above."""
    if value is None:
        return "—"
    return f"${value:,.0f}"


def money_cents(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


# --------------------------------------------------------------------------
# Markdown-safe output
#
# st.markdown / st.caption / st.info treat text between two dollar signs as
# LaTeX. "$59 of $371,924" therefore rendered as green italic maths with the
# dollar signs eaten. Escaping each $ as \$ is the documented fix.
#
# This does NOT apply to the HTML components above: content inside a raw
# <div>/<span> is not run through the LaTeX pass, and escaping there would
# print a literal backslash. So money() stays plain and md_money() is the one
# to use in any markdown, caption, info or warning string.
# --------------------------------------------------------------------------

def md_escape(text: object) -> str:
    """Escape dollar signs so Streamlit does not read them as LaTeX.

    Idempotent. md_money() escapes, and md() escapes the whole string it is
    handed - so a string built from md_money() parts would otherwise be escaped
    twice and render a literal backslash. Only a $ that is not already escaped
    is touched.
    """
    return re.sub(r"(?<!\\)\$", r"\\$", str(text))


def md_money(value: float | None) -> str:
    return md_escape(money(value))


def md_money_cents(value: float | None) -> str:
    return md_escape(money_cents(value))


def md(text: str, **kwargs) -> None:
    """st.markdown with dollar signs escaped."""
    st.markdown(md_escape(text), **kwargs)


def md_caption(text: str, **kwargs) -> None:
    st.caption(md_escape(text), **kwargs)


def md_info(text: str, **kwargs) -> None:
    st.info(md_escape(text), **kwargs)


def md_warning(text: str, **kwargs) -> None:
    st.warning(md_escape(text), **kwargs)


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def share_pct(value: float | None) -> str:
    """A share, never rounded to a misleading 0.0%.

    A $59 item against $372k is 0.0157%. Printing "0.0%" reads as "nothing",
    which is the wrong impression for a real item - it is small, not absent.
    """
    if value is None:
        return "—"
    if value == 0:
        return "0%"
    if 0 < value < 0.1:
        return "<0.1%"
    if -0.1 < value < 0:
        return ">-0.1%"
    return f"{value:.1f}%"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """'1 account-day' / '2 account-days'."""
    word = singular if count == 1 else (plural_form or f"{singular}s")
    return f"{count:,} {word}"


def direction_of(value: float | None) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else ("down" if value < 0 else "flat")


# --------------------------------------------------------------------------
# Table formatting
#
# Values go into st.dataframe as numbers and are formatted by a pandas Styler,
# so sorting still works on the underlying number while the cell reads as
# money. Dataframes do not run the markdown pass, so a plain "$" is safe here -
# no escaping needed, unlike st.markdown.
# --------------------------------------------------------------------------

COLOR_UP = "#1a7f37"
COLOR_DOWN = "#c0392b"


def signed_money(value: float | None) -> str:
    """+$717 / -$2,055."""
    if value is None or pd.isna(value):
        return "—"
    return f"{'+' if value >= 0 else '-'}${abs(value):,.0f}"


def signed_pct(value: float | None) -> str:
    """+11.6% / -49.9%."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value:+.1f}%"


def plain_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.0f}"


def plain_money_cents(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.2f}"


def as_int(value: float | None) -> str:
    """A count. Stays a float in the frame because NULLs live in the same
    column (per-type scorecard rows carry no FP), so it must be formatted
    explicitly or it renders as 855.000000."""
    if value is None or pd.isna(value):
        return "—"
    return f"{int(round(value)):,}"


def num(value: float | None, places: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.{places}f}"


def color_sign(value) -> str:
    """Green for positive, red for negative, nothing for zero or missing."""
    if value is None or pd.isna(value) or value == 0:
        return ""
    return f"color: {COLOR_UP}" if value > 0 else f"color: {COLOR_DOWN}"


def styled_table(df, formats: dict, color_columns: list[str] | None = None):
    """A Styler with per-column formatting and optional sign colouring.

    Styler.map replaced Styler.applymap in pandas 2.1; Snowflake's channel
    ships 2.x, but the fallback keeps this working on an older runtime rather
    than failing at render time.
    """
    styler = df.style.format(formats, na_rep="—")
    if color_columns:
        present = [c for c in color_columns if c in df.columns]
        if present:
            apply_elementwise = getattr(styler, "map", None) or styler.applymap
            styler = apply_elementwise(color_sign, subset=present)
    return styler
