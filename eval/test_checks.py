"""Regression suite for the summary checkers. No Snowflake, no API calls.

    python eval/test_checks.py

Exists because the checkers were wrong more often than the model was: five
false positives against three real catches during development. Every case below
is either a real sentence from a generated summary or a minimal injection, and
roughly half assert that a CORRECT sentence stays clean - a checker that fires
on good output gets switched off as fast as one that catches nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from common.quiet_streamlit import silence  # noqa: E402
silence()
sys.path.insert(0, str(REPO_ROOT / "app"))

from claims import check_claims  # noqa: E402
from verifier import extract_numbers, strip_list_marker, verify  # noqa: E402

PAYLOAD = {
    "week_start": "2026-10-05",
    "revenue_reported": 482972.64,
    "prior_week_revenue": 358801.04,
    "wow_pct": 34.6,
    "wow_change_usd": 124171.60,
    "trailing_4w_avg": 343682.60,
    "active_accounts": 184,
    "under_review_usd": 134035.95,
    "under_review_account_days": 1,
    "review_queue_items": 1,
    "revenue_excluding_items_under_review": 348936.69,
    "revenue_by_product": {"ai_services": 281529.95, "compute": 137547.60},
    "revenue_by_segment": {"Commercial": 254260.71, "Enterprise": 188249.32},
    "page_tier_items": [
        {"check": "unit_error", "account_id": "ACC00072",
         "date": "2026-10-07", "impact_usd": 133703.16},
    ],
    "movers_basis": "excluding_under_review",
    "top_movers_up": [
        {"account_id": "ACC00164", "change_usd": 1638.12, "change_pct": 17.0,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
        {"account_id": "ACC00123", "change_usd": 717.07, "change_pct": 11.6,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
    ],
    "top_movers_down": [
        {"account_id": "ACC00155", "change_usd": -873.05, "change_pct": -12.3,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
        {"account_id": "ACC00193", "change_usd": -402.93, "change_pct": -6.8,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
        {"account_id": "ACC00128", "change_usd": -790.70, "change_pct": -7.2,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
        {"account_id": "ACC00086", "change_usd": -670.60, "change_pct": -6.1,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
        {"account_id": "ACC00150", "change_usd": -659.70, "change_pct": -5.9,
         "prior_week_under_review_usd": 0.0, "prior_week_issue_type": None},
        {"account_id": "ACC00008", "change_usd": -13991.95, "change_pct": -95.8,
         "prior_week_under_review_usd": 14088.37,
         "prior_week_issue_type": "unit_error"},
    ],
}

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if condition else 'FAIL'}  {name}")
    if not condition:
        failures.append(f"{name}{' - ' + detail if detail else ''}")
        if detail:
            print(f"          {detail}")


def numbers_in(text: str) -> dict[str, dict]:
    return {item["text"]: item for item in extract_numbers(text)}


print("extraction")
check("list marker stripped",
      strip_list_marker("- $134,035.95 is under review").startswith("$"))
found = numbers_in("- $134,035.95 is under review on 1 account-day")
check("list dash is not read as a minus",
      found.get("$134,035.95", {}).get("sign") is None,
      f"sign={found.get('$134,035.95', {}).get('sign')}")
check("'4-week average' does not yield the integer 4",
      "4" not in numbers_in("+40.5% above the trailing 4-week average of $343,682"))
check("date fragments are not quantities",
      not {"2026", "10"} & set(numbers_in("on 2026-10-07 the load failed")))
check("identifier digits are not quantities",
      "00072" not in numbers_in("ACC00072 was flagged"))

print("\nsign")
signed = numbers_in("- ACC00155 fell $873.05 this week")
check("a movement word asserts a negative", signed["$873.05"]["sign"] == -1)
two = numbers_in("- down 1.4% from $377,378")
check("a movement word governs only the first number after it",
      two["1.4%"]["sign"] == -1 and two["$377,378"]["sign"] is None,
      f"1.4%={two['1.4%']['sign']}, $377,378={two['$377,378']['sign']}")

print("\nmatching: a flipped sign must fail")
good = "- Reported revenue was $482,973, up +34.6% from $358,801 the prior week."
check("correct sentence verifies", verify(good, PAYLOAD).ok)
check("flipped percentage fails",
      not verify(good.replace("+34.6%", "-34.6%"), PAYLOAD).ok)
check("flipped movement word fails",
      not verify("- ACC00164 fell $1,638.12 this week.", PAYLOAD).ok)
check("correct movement word passes",
      verify("- ACC00164 rose $1,638.12 this week.", PAYLOAD).ok)

print("\nmatching: small integers pick a field from context")
res = verify("- $134,036 is under review on 1 account-day, and 1 item is in "
             "the review queue.", PAYLOAD)
fields = {n.text: n.matched_against for b in res.bullets for n in b.numbers}
ones = [n for b in res.bullets for n in b.numbers if n.text == "1"]
check("the two 1s map to different fields",
      len({o.matched_against for o in ones}) == 2,
      f"{[o.matched_against for o in ones]}")
check("account-day 1 maps to under_review_account_days",
      ones[0].matched_against == "under_review_account_days",
      str(ones[0].matched_against))
check("review-queue 1 maps to review_queue_items",
      ones[1].matched_against == "review_queue_items",
      str(ones[1].matched_against))
check("a large figure prefers the nearest field, not the first hit",
      fields.get("$134,036") == "under_review_usd", str(fields.get("$134,036")))

res2 = verify("- There were 7 incidents this week.", PAYLOAD)
seven = [n for b in res2.bullets for n in b.numbers if n.text == "7"]
check("an unsupported small integer is low confidence, not matched",
      seven and seven[0].confidence == "low_confidence",
      seven[0].confidence if seven else "not extracted")

print("\nclaim checks: correct sentences must stay clean")
clean_cases = [
    ("bare list item across a sentence break",
     "- The other four fallers were ACC00128, ACC00086, ACC00150 and ACC00193, "
     "all Enterprise. The largest riser was ACC00123 at +$717."),
    ("conjoined riser and faller",
     "- ACC00164 (Enterprise, +$1,638.12) was the largest riser and ACC00155 "
     "(Enterprise, -$873.05) was the largest faller."),
    ("account named as a data-quality item, not a mover",
     "- $134,035.95 is under review for a unit_error, almost all from ACC00072."),
    ("leader named correctly",
     "- ai_services led by product at $281,530."),
]
for name, text in clean_cases:
    errors = [i for i in check_claims(text, PAYLOAD) if i.severity == "error"]
    check(name, not errors, "; ".join(i.detail[:70] for i in errors))

print("\nclaim checks: wrong claims must fire")
firing_cases = [
    ("rebound given a commercial reading (the real ACC00008 sentence)",
     "- SMB account ACC00008 fell $13,991.95 (-95.8%) to $606.41 and is worth "
     "a churn check."),
    ("direction contradicted by a movement word",
     "- ACC00155 rose $873.05 on strong demand."),
    ("direction contradicted by an adjacent sign",
     "- ACC00164 (Enterprise, -$1,638.12) was the largest faller."),
    ("fabricated account",
     "- ACC09999 was the largest riser, up $717."),
    ("wrong leader",
     "- compute led by product at $137,548."),
    ("superlative the aggregates cannot rank",
     "- ACC00123 posted the sharpest percentage jump at 11.6%."),
]
for name, text in firing_cases:
    errors = [i for i in check_claims(text, PAYLOAD) if i.severity == "error"]
    check(name, bool(errors))

print()
if failures:
    print(f"{len(failures)} failure(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks behave as intended")
