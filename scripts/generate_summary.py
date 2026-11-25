"""Generate a weekly executive summary and store it in DQ.AI_SUMMARY.

    python scripts/generate_summary.py 2026-11-16
    python scripts/generate_summary.py 2026-11-16 --backend template
    SUMMARY_BACKEND=cortex python scripts/generate_summary.py 2026-11-16

Runs locally, not in Snowflake. Snowflake Cortex AI_COMPLETE is unavailable on
a trial account - every model returns "AI function
_COMPLETE_WITH_PROMPT_HISTORY_LLM is not available for trial accounts" - so the
default backend calls the Anthropic API from here and writes the result back.
The cortex backend is kept behind the same interface so this runs natively the
day the account is upgraded. See DECISIONS.md part 10.

Backends (SUMMARY_BACKEND, or --backend):

  anthropic  Anthropic API, model claude-opus-5-5. Needs ANTHROPIC_API_KEY.
  cortex     Snowflake AI_COMPLETE, model from CORTEX_MODEL. Non-trial only.
  template   No model at all - fills a fixed sentence per bullet from the same
             JSON. For testing the pipeline and the verifier without an API
             key, and as a graceful degradation. Its numbers come straight from
             the JSON, so it passes the verifier by construction; that proves
             the plumbing, not the verifier's ability to catch a bad number.

Only aggregates are sent to the model - no account-level rows beyond the named
top movers, no event rows. The exact JSON is stored alongside the summary so
the app can verify every number in the text against it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
import snowflake.connector  # noqa: E402

# The app's queries use "?" placeholders, which is what Snowpark takes. The
# connector defaults to pyformat, so switch it before connecting - otherwise
# every reused query fails to bind.
snowflake.connector.paramstyle = "qmark"

from common.quiet_streamlit import silence as quiet_streamlit  # noqa: E402
from common.snowflake_conn import ConfigError, connect  # noqa: E402

#: Default model. Sonnet 5.5 was measured against Opus 5.5 and Haiku 5.5 on
#: this exact task (eval/compare_models.py): identical verification pass rate,
#: the lowest latency of the three, and 39% of Opus's cost. Opus is the better
#: default for harder work; for a five-bullet brief off a 2.6KB JSON it buys
#: nothing measurable here.
ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_MAX_TOKENS = 2048

#: Per-model request shape and price. The request is NOT the same across
#: models, which is the main reason this table exists rather than one set of
#: keyword arguments:
#:
#:   effort      output_config.effort is rejected on Haiku 4.5, so it is only
#:               sent where it is supported. A five-bullet brief off a 2KB JSON
#:               is a simple task, so "low" is right where it applies -
#:               Opus 5.5 would otherwise default to "medium".
#:   fallbacks   server-side refusal fallbacks are available across the 5.x
#:               line, not on Haiku 4.5.
#:   thinking    omitted everywhere: it is always on and adaptive on the 5.x
#:               models, and off by default on Haiku 4.5, which is what a task
#:               this small wants.
#:
#: Shapes were probed against the API rather than assumed - Haiku 5.5 accepts
#: effort, adaptive thinking and fallbacks, exactly like Opus and Sonnet 5.5.
#:
#: Prices are USD per million tokens as (input, output), read from
#: platform.claude.com/docs/en/about-claude/pricing on 2026-11-25.
#:
#: Haiku 5.5 is priced BY PROMPT LENGTH: a request whose prompt exceeds
#: PROMPT_TIER_TOKENS pays 5x the input rate and 5x the output rate. Prompt
#: length counts all input tokens including cache reads and writes, and each
#: request is priced on its own. `price_long` carries that second tier; a model
#: without one is flat-rated.
PROMPT_TIER_TOKENS = 100_000

MODEL_CONFIG = {
    "claude-opus-5-5":   {"effort": "low", "fallbacks": True,
                          "price": (4.00, 20.00)},
    "claude-sonnet-5-5": {"effort": "low", "fallbacks": True,
                          "price": (2.00, 10.00)},
    "claude-haiku-5-5":  {"effort": "low", "fallbacks": True,
                          "price": (0.10, 0.50),
                          "price_long": (0.50, 2.50)},
    # A generation behind, and the one model here with a different request
    # shape: it rejects output_config.effort and server-side fallbacks.
    "claude-haiku-4-5":  {"effort": None,  "fallbacks": False,
                          "price": (1.00, 5.00)},
}


def price_for(config: dict, input_tokens: int) -> tuple[float, float]:
    """(input, output) rate per MTok for a request of this prompt length."""
    if config.get("price_long") and input_tokens > PROMPT_TIER_TOKENS:
        return config["price_long"]
    return config["price"]


TABLE = "REVENUE_PULSE.DQ.AI_SUMMARY"

SYSTEM_PROMPT = """You write a weekly revenue brief for a finance and data team.

Rules, in order of importance:

1. Use ONLY numbers that appear in the JSON you are given. Never compute a new
   number, never round to a different precision, never estimate, and never
   carry a number over from general knowledge. Every figure in your output is
   checked automatically against the JSON and the brief is rejected if a number
   does not appear there.

2. If you want to make a point that needs a number the JSON does not contain,
   make the point qualitatively instead, with no number.

3. NO SUPERLATIVES BEYOND THE DATA. The movers lists are the top five by
   DOLLAR change in each direction - nothing else. So you may say an account
   was the largest riser or faller by dollars. You may NOT say anything was the
   "sharpest percentage jump", the "biggest percentage move", the "fastest
   growing" or similar, because the JSON does not rank by percentage and you
   cannot see the accounts outside the top five. The same applies to products
   and segments: say which led, not that one "grew fastest".

4. ABSENCE OF A FLAG IS NOT PROOF THE MOVEMENT IS REAL. When nothing is under
   review, say that no data-quality issues were flagged. Do NOT write that the
   movement "reflects actual consumption", "is genuine", "is real demand" or
   anything equivalent - the checks cover specific known failure modes, not
   every possible fault.

5. A MOVER WITH prior_week_under_review_usd ABOVE ZERO IS A DATA-QUALITY
   REBOUND, NOT BUSINESS MOVEMENT. Its prior week was distorted by the named
   prior_week_issue_type, so this week's change is largely the return to
   normal. Describe it that way, naming the issue type. Never suggest churn
   risk, a lost customer, a win, or any commercial explanation for such a
   move, and never cite it as evidence of a trend.

6. The movers lists are computed on the basis named in movers_basis. When that
   is "excluding_under_review", account-days under review were removed from
   both weeks before ranking, so these are the movers with known data-quality
   effects already taken out. Say so if you discuss the mover list.

7. MONEY FORMAT: a dollar sign and thousands separators, rounded to WHOLE
   DOLLARS, or to one decimal in thousands - "$482,973" or "$483.0K". Never
   write cents: not "$482,972.64", not "371924.01". Round the figure you were
   given; do not compute a new one. Percentages keep a sign and one decimal:
   +2.1%, -1.4%.

8. Exactly five bullets, each one line, each starting with "- ".

9. Say what moved, which accounts or products drove it, and whether any of the
   movement is a data-quality problem rather than real consumption. The JSON
   separates revenue under review from revenue as reported - if a material
   amount is under review, say so and name the issue.

9b. WHEN under_review_is_material IS TRUE you MUST also state which way revenue
   went EXCLUDING the items under review, using adjusted_direction and
   adjusted_wow_pct. A reader who only sees the reported headline draws the
   wrong conclusion, so "reported revenue rose 34.6%, but excluding items under
   review it rose 1.2%" is the shape required. Note that adjusted_wow_pct
   compares the adjusted week against the ADJUSTED prior week - both on the
   same basis. Do not compare the adjusted figure against the reported prior
   week; that mixes bases and can reverse the apparent direction.

10. Plain language. No preamble, no closing line, no headings. Bullets only."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------

class _ConnectorSession:
    """Adapter giving app/queries.py the one method it needs, backed by the
    snowflake-connector connection this script already holds."""

    def __init__(self, conn):
        self._conn = conn

    class _Result:
        def __init__(self, cur):
            self._cur = cur

        def to_pandas(self):
            return self._cur.fetch_pandas_all()

    def sql(self, sql: str, params: list | None = None):
        cur = self._conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        return self._Result(cur)


def build_payload(conn, week_start: date) -> dict:
    """Aggregates only. No raw event rows, no account-level detail beyond movers."""
    sys.path.insert(0, str(REPO_ROOT / "app"))

    def q(sql: str, params: list | None = None) -> pd.DataFrame:
        cur = conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetch_pandas_all()

    wk = str(week_start)

    totals = q(
        """
        WITH by_week AS (
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WEEK_START,
                   SUM(REVENUE) AS REVENUE,
                   COUNT(DISTINCT USAGE_DATE) AS DAYS_PRESENT,
                   COUNT(DISTINCT ACCOUNT_ID) AS ACTIVE_ACCOUNTS
            FROM REVENUE_PULSE.MARTS.FCT_DAILY_REVENUE GROUP BY 1
        ),
        full_weeks AS (SELECT * FROM by_week WHERE DAYS_PRESENT = 7),
        ranked AS (
            SELECT WEEK_START, REVENUE, ACTIVE_ACCOUNTS,
                   LAG(REVENUE) OVER (ORDER BY WEEK_START) AS PRIOR_REVENUE,
                   AVG(REVENUE) OVER (ORDER BY WEEK_START
                                      ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS TRAILING_4W
            FROM full_weeks
        )
        SELECT * FROM ranked WHERE WEEK_START = ?
        """,
        [wk],
    )
    if totals.empty:
        raise SystemExit(
            f"{wk} is not a complete week in MARTS.FCT_DAILY_REVENUE. "
            "Summaries are only generated for complete weeks (DECISIONS.md part 7)."
        )
    t = totals.iloc[0]
    revenue = float(t["REVENUE"])
    prior = None if pd.isna(t["PRIOR_REVENUE"]) else float(t["PRIOR_REVENUE"])
    trailing = None if pd.isna(t["TRAILING_4W"]) else float(t["TRAILING_4W"])

    def pct(cur_v, base):
        if base in (None, 0):
            return None
        return round((cur_v - base) / base * 100, 1)

    by_product = q(
        """
        SELECT PRODUCT, ROUND(SUM(REVENUE), 2) AS REVENUE
        FROM REVENUE_PULSE.MARTS.FCT_DAILY_REVENUE
        WHERE DATE_TRUNC('week', USAGE_DATE)::date = ? GROUP BY 1 ORDER BY 2 DESC
        """, [wk])
    by_segment = q(
        """
        SELECT SEGMENT, ROUND(SUM(REVENUE), 2) AS REVENUE
        FROM REVENUE_PULSE.MARTS.FCT_DAILY_REVENUE
        WHERE DATE_TRUNC('week', USAGE_DATE)::date = ? GROUP BY 1 ORDER BY 2 DESC
        """, [wk])

    # Before the import: the cache warnings fire as @st.cache_data decorates
    # each function in queries.py, so silencing afterwards is too late.
    quiet_streamlit()
    # Before the import: the cache warnings fire as @st.cache_data decorates
    # each function in queries.py, so silencing afterwards is too late.
    quiet_streamlit()
    import queries as app_q  # reuse the app's definitions so both agree
    app_q.use_session(_ConnectorSession(conn))

    # Movers on the EXCLUDING-UNDER-REVIEW basis. On the reported basis the two
    # largest movers for 2026-10-05 were both data-quality artefacts - the unit
    # error itself (+$133,750) and the same account's return to normal the
    # following week (-$13,992) - which pushed two real movers out of the list.
    # See DECISIONS.md part 11.
    movers = app_q.top_movers(week_start, limit=5, basis="excluding_under_review")
    under = app_q.under_review(week_start)
    alerts = app_q.page_tier_alerts(week_start)
    queue = app_q.review_queue(week_start)

    # Second line of defence: annotate each mover with what was under review in
    # its PRIOR week. The adjusted basis only removes days that were flagged;
    # an account recovering from an unflagged problem would still show up, and
    # the prompt and the claim checks both use this field.
    prior_week = week_start - timedelta(days=7)
    prior_flags = app_q.flagged_account_days(prior_week)
    prior_by_account: dict[str, dict] = {}
    for r in prior_flags.itertuples():
        entry = prior_by_account.setdefault(
            r.ACCOUNT_ID, {"usd": 0.0, "issues": set()}
        )
        entry["usd"] += float(r.REVENUE_AT_RISK)
        for issue in str(r.ISSUES).split(","):
            entry["issues"].add(issue.strip())

    def mover_rows(direction):
        sub = movers[movers["DIRECTION"] == direction]
        rows = []
        for r in sub.itertuples():
            flagged = prior_by_account.get(r.ACCOUNT_ID)
            rows.append({
                "account_id": r.ACCOUNT_ID,
                "segment": r.SEGMENT,
                "revenue": round(float(r.REVENUE), 2),
                "change_usd": round(float(r.CHANGE_USD), 2),
                "change_pct": None if pd.isna(r.CHANGE_PCT) else float(r.CHANGE_PCT),
                "prior_week_under_review_usd": (
                    round(flagged["usd"], 2) if flagged else 0.0
                ),
                "prior_week_issue_type": (
                    ", ".join(sorted(flagged["issues"])) if flagged else None
                ),
            })
        return rows

    # Adjusted figures, both weeks on the same basis.
    adjusted = revenue - float(under["amount"])
    prior_under_review = float(app_q.under_review(prior_week)["amount"])
    adjusted_prior = None if prior is None else prior - prior_under_review
    adjusted_wow = pct(adjusted, adjusted_prior)
    if adjusted_wow is None:
        adjusted_direction = None
    elif adjusted_wow > 0:
        adjusted_direction = "up"
    elif adjusted_wow < 0:
        adjusted_direction = "down"
    else:
        adjusted_direction = "flat"

    from claims import is_material           # single definition of materiality
    material = bool(is_material(float(under["amount"]), revenue))

    issues = []
    for r in alerts.itertuples():
        issues.append({
            "check": r.CHECK_NAME,
            "account_id": r.ACCOUNT_ID,
            "date": str(r.EVENT_DATE),
            "impact_usd": None if pd.isna(r.IMPACT_USD) else round(float(r.IMPACT_USD), 2),
        })

    return {
        "week_start": wk,
        "revenue_reported": round(revenue, 2),
        "prior_week_revenue": None if prior is None else round(prior, 2),
        "wow_pct": pct(revenue, prior),
        # The dollar deltas are supplied, not left to be derived. Haiku 4.5 was
        # observed writing $124,171.60 (reported - prior) and $139,290.04
        # (reported - trailing average) - correct arithmetic, but figures the
        # payload did not contain, which rule 1 forbids. Providing them removes
        # the need to compute anything.
        "wow_change_usd": None if prior is None else round(revenue - prior, 2),
        "trailing_4w_avg": None if trailing is None else round(trailing, 2),
        "vs_trailing_4w_pct": pct(revenue, trailing),
        "vs_trailing_4w_change_usd": (
            None if trailing is None else round(revenue - trailing, 2)
        ),
        "active_accounts": int(t["ACTIVE_ACCOUNTS"]),
        "revenue_by_product": {r.PRODUCT: float(r.REVENUE) for r in by_product.itertuples()},
        "revenue_by_segment": {r.SEGMENT: float(r.REVENUE) for r in by_segment.itertuples()},
        "movers_basis": "excluding_under_review",
        "top_movers_up": mover_rows("up"),
        "top_movers_down": mover_rows("down"),
        # The adjusted comparison is adjusted-vs-ADJUSTED-prior, not
        # adjusted-vs-reported-prior. The prior week usually has its own items
        # under review, and comparing a cleaned figure against an uncleaned one
        # is a basis mismatch - the same error as comparing a partial week with
        # a full one (DECISIONS.md part 7). For 2026-10-05 the two bases
        # disagree about the DIRECTION: +1.2% like-for-like against -2.8% on
        # mismatched bases. Only the honest one is exposed, because exposing
        # the other invites the mistake.
        "adjusted_prior_week_revenue": (
            None if prior is None else round(prior - prior_under_review, 2)
        ),
        "adjusted_wow_pct": adjusted_wow,
        "adjusted_direction": adjusted_direction,
        "under_review_is_material": material,
        "under_review_usd": round(float(under["amount"]), 2),
        "under_review_account_days": int(under["account_days"]),
        "under_review_issues": under["issues"],
        "revenue_excluding_items_under_review": round(revenue - float(under["amount"]), 2),
        "page_tier_items": issues,
        "review_queue_items": int(len(queue)),
    }


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

def summarise_anthropic(payload: dict, model: str = ANTHROPIC_MODEL) -> tuple[str, str, dict]:
    """Returns (summary, model label, usage). Usage carries tokens, latency, cost."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("pip install anthropic, or use --backend template")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set in .env.\n"
            "Add it, or run with --backend template to exercise the pipeline "
            "and the verifier without an API key."
        )

    config = MODEL_CONFIG.get(model)
    if config is None:
        raise SystemExit(
            f"unknown model {model!r}. Known: {', '.join(sorted(MODEL_CONFIG))}"
        )

    client = anthropic.Anthropic()
    user = (
        "Write the weekly brief from this JSON. Use only numbers that appear "
        "in it.\n\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    )

    kwargs: dict = {
        "model": model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
    }
    if config["effort"]:
        kwargs["output_config"] = {"effort": config["effort"]}
    if config["fallbacks"]:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"

    started = time.perf_counter()
    response = client.beta.messages.create(**kwargs)
    elapsed = time.perf_counter() - started

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        raise SystemExit(f"the model declined this request (category: {detail})")

    text = "\n".join(b.text for b in response.content if b.type == "text").strip()

    in_tokens = response.usage.input_tokens
    out_tokens = response.usage.output_tokens
    in_price, out_price = price_for(config, in_tokens)
    usage = {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "latency_s": round(elapsed, 2),
        "cost_usd": round(
            in_tokens / 1e6 * in_price + out_tokens / 1e6 * out_price, 6
        ),
        "price_tier": ("long" if config.get("price_long")
                       and in_tokens > PROMPT_TIER_TOKENS else "standard"),
        "served_by": response.model,
    }
    return text, f"anthropic:{response.model}", usage


def summarise_cortex(conn, payload: dict) -> tuple[str, str]:
    """Snowflake-native path. Requires a non-trial account."""
    model = os.environ.get("CORTEX_MODEL", "claude-4-sonnet")
    prompt = SYSTEM_PROMPT + "\n\nJSON:\n" + json.dumps(payload, default=str)
    cur = conn.cursor()
    try:
        cur.execute("SELECT AI_COMPLETE(?, ?)", [model, prompt])
        return (cur.fetchone()[0] or "").strip(), f"cortex:{model}"
    except Exception as exc:
        raise SystemExit(
            f"Cortex AI_COMPLETE failed: {str(exc).splitlines()[0]}\n"
            "On a trial account every model is blocked. Use --backend anthropic."
        )


def summarise_template(payload: dict) -> tuple[str, str]:
    """No model. Numbers are copied from the JSON, so it always verifies."""
    p = payload
    top_product = max(p["revenue_by_product"], key=p["revenue_by_product"].get)
    top_segment = max(p["revenue_by_segment"], key=p["revenue_by_segment"].get)
    up = p["top_movers_up"][0] if p["top_movers_up"] else None
    down = p["top_movers_down"][0] if p["top_movers_down"] else None

    bullets = [
        f"- Revenue for the week beginning {p['week_start']} was "
        f"${p['revenue_reported']:,.2f}, {p['wow_pct']:+.1f}% against the prior week.",
        f"- That is {p['vs_trailing_4w_pct']:+.1f}% against the trailing four-week "
        f"average of ${p['trailing_4w_avg']:,.2f}, across {p['active_accounts']} active accounts.",
        f"- {top_product} led by product at ${p['revenue_by_product'][top_product]:,.2f}, "
        f"and {top_segment} led by segment at ${p['revenue_by_segment'][top_segment]:,.2f}.",
        (f"- Largest riser {up['account_id']} added ${up['change_usd']:,.2f}; "
         f"largest faller {down['account_id']} lost ${abs(down['change_usd']):,.2f}."
         if up and down else "- No week-over-week movers available."),
        (f"- ${p['under_review_usd']:,.2f} is under review across "
         f"{p['under_review_account_days']} account-day(s) ({p['under_review_issues']}), "
         f"leaving ${p['revenue_excluding_items_under_review']:,.2f} excluding those items."
         if p["under_review_usd"] else
         "- Nothing is under review this week; the reported figure stands."),
    ]
    return "\n".join(bullets), "template:none"


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def store(conn, week_start: date, model: str, payload: dict, summary: str) -> None:
    """Replace the stored summary for this week.

    A single-row INSERT rather than write_pandas: staging a one-row frame put a
    pandas Timestamp into the DATE column and Snowflake refused the cast, and
    binding the date as a string is both simpler and faster here.
    """
    prompt_hash = hashlib.sha256(
        (SYSTEM_PROMPT + json.dumps(payload, sort_keys=True, default=str)).encode()
    ).hexdigest()[:32]

    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            WEEK_START   DATE,
            MODEL        VARCHAR,
            PROMPT_HASH  VARCHAR,
            INPUT_JSON   VARCHAR,
            SUMMARY      VARCHAR,
            CREATED_AT   TIMESTAMP_NTZ
        )
    """)
    # One summary per week: regenerating replaces rather than accumulates.
    cur.execute(f"DELETE FROM {TABLE} WHERE WEEK_START = TO_DATE(?)", [str(week_start)])
    cur.execute(
        f"""
        INSERT INTO {TABLE}
            (WEEK_START, MODEL, PROMPT_HASH, INPUT_JSON, SUMMARY, CREATED_AT)
        SELECT TO_DATE(?), ?, ?, ?, ?, TO_TIMESTAMP_NTZ(?)
        """,
        [
            str(week_start),
            model,
            prompt_hash,
            json.dumps(payload, default=str),
            summary,
            utc_now().strftime("%Y-%m-%d %H:%M:%S"),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("week_start", help="Monday of a complete week, YYYY-MM-DD")
    parser.add_argument("--backend", choices=["anthropic", "cortex", "template"],
                        default=os.environ.get("SUMMARY_BACKEND", "anthropic"))
    parser.add_argument("--model", default=ANTHROPIC_MODEL,
                        choices=sorted(MODEL_CONFIG),
                        help="Anthropic model (ignored by the other backends)")
    args = parser.parse_args()
    week_start = date.fromisoformat(args.week_start)

    try:
        conn = connect(schema="DQ")
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        payload = build_payload(conn, week_start)
        print(f"Built aggregates for {week_start}: "
              f"{len(json.dumps(payload))} bytes, "
              f"{len(payload['top_movers_up'])} up / "
              f"{len(payload['top_movers_down'])} down movers, "
              f"{len(payload['page_tier_items'])} page-tier item(s)")

        usage: dict = {}
        if args.backend == "anthropic":
            summary, model, usage = summarise_anthropic(payload, args.model)
        elif args.backend == "cortex":
            summary, model = summarise_cortex(conn, payload)
        else:
            summary, model = summarise_template(payload)

        store(conn, week_start, model, payload, summary)
        print(f"\nmodel: {model}")
        if usage:
            print(f"tokens: {usage['input_tokens']:,} in / "
                  f"{usage['output_tokens']:,} out · "
                  f"{usage['latency_s']}s · ${usage['cost_usd']:.6f}")
        print()
        print(summary)
        print(f"\nStored in {TABLE} for week {week_start}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
