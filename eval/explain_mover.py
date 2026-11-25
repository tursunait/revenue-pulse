"""Explain a mover by checking it against ANOMALY_LABELS.

    python eval/explain_mover.py ACC00008 2026-10-05

Lives in eval/ because it reads RAW.ANOMALY_LABELS. Neither the detector, the
app, nor the summary generator may read that table - this is a review tool for
answering "was that move real?" after the fact.

Written because a generated summary described a 95.8% fall as "worth a churn
check" when the account had simply returned to normal after a planted anomaly
inflated the week before (DECISIONS.md part 11).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.snowflake_conn import connect  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    account_id, week = sys.argv[1], date.fromisoformat(sys.argv[2])
    prior = week - timedelta(days=7)

    conn = connect(schema="DQ")
    try:
        cur = conn.cursor()
        print(f"{account_id}: week {week} vs prior week {prior}\n")

        cur.execute(
            """
            SELECT DATE_TRUNC('week', USAGE_DATE)::date AS WK,
                   ROUND(SUM(REVENUE), 2) AS REVENUE
            FROM REVENUE_PULSE.MARTS.FCT_DAILY_REVENUE
            WHERE ACCOUNT_ID = %s
              AND USAGE_DATE >= DATEADD('week', -3, %s::date)
              AND USAGE_DATE <  DATEADD('week', 2, %s::date)
            GROUP BY 1 ORDER BY 1
            """,
            (account_id, str(prior), str(week)),
        )
        print("  weekly revenue around the move:")
        for wk, rev in cur.fetchall():
            tag = "  <- the move" if wk == week else ("  <- prior week" if wk == prior else "")
            print(f"     {wk}  ${rev:>12,.2f}{tag}")

        cur.execute(
            """
            SELECT LABEL_ID, ANOMALY_TYPE, PRODUCT,
                   TO_VARCHAR(START_DATE, 'YYYY-MM-DD'),
                   TO_VARCHAR(END_DATE, 'YYYY-MM-DD'),
                   MAGNITUDE, DESCRIPTION
            FROM REVENUE_PULSE.RAW.ANOMALY_LABELS
            WHERE ACCOUNT_ID = %s ORDER BY START_DATE
            """,
            (account_id,),
        )
        labels = cur.fetchall()
        print(f"\n  planted anomalies for this account: {len(labels)}")
        for lid, kind, product, d0, d1, mag, desc in labels:
            span = d0 if d0 == d1 else f"{d0}..{d1}"
            in_prior = prior <= date.fromisoformat(d0) < week
            in_week = week <= date.fromisoformat(d0) < week + timedelta(days=7)
            where = ("  <- PRIOR WEEK" if in_prior else
                     "  <- THIS WEEK" if in_week else "")
            print(f"     {lid} {kind:20} {product or '(all)':12} {span:22} "
                  f"x{mag:<10,.2f}{where}")
            if in_prior or in_week:
                print(f"        {desc}")

        cur.execute(
            """
            SELECT TO_VARCHAR(ACTIVITY_DATE, 'YYYY-MM-DD'), BREAK_KIND,
                   ROUND(USAGE_REVENUE, 2), ROUND(LEDGER_REVENUE, 2),
                   ROUND(ABS_DIFF_USD, 2)
            FROM REVENUE_PULSE.DQ.DQ_RECONCILIATION
            WHERE ACCOUNT_ID = %s
              AND ACTIVITY_DATE >= DATEADD('week', -1, %s::date)
              AND ACTIVITY_DATE <  DATEADD('week', 1, %s::date)
            ORDER BY 1
            """,
            (account_id, str(prior), str(week)),
        )
        breaks = cur.fetchall()
        print(f"\n  reconciliation breaks in those two weeks: {len(breaks)}")
        for d, kind, usage, ledger, diff in breaks:
            print(f"     {d}  {kind:16} usage ${usage:>12,.2f}  "
                  f"ledger ${ledger:>10,.2f}  diff ${diff:>12,.2f}")

        print("\n  verdict:", end=" ")
        prior_flagged = any(
            prior <= date.fromisoformat(d0) < week for _, _, _, d0, _, _, _ in labels
        )
        if prior_flagged:
            print("the PRIOR week carried a planted anomaly — a fall here is a "
                  "return to normal, not business movement.")
        else:
            print("no anomaly in the prior week — the move is not explained by "
                  "a data-quality rebound.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
