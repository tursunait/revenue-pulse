"""Map every DQ exception to its ANOMALY_LABELS row, and report what was missed.

This lives in eval/ because it reads RAW.ANOMALY_LABELS. The DQ models and any
detector must never read that table - only eval/ may.

    python eval/triage_dq.py

Prints, for each DQ model:
  * every exception row, matched to the planted anomaly that explains it
  * any exception with no matching label - a candidate false positive or bug
  * any planted anomaly no DQ model caught - a candidate false negative
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.snowflake_conn import ConfigError, connect  # noqa: E402

SCHEMA = "DQ"

#: Which DQ model is expected to catch which anomaly types. Used only to say
#: whether a miss is by design, never to filter the matching itself.
EXPECTED_CATCHER = {
    "dq_reconciliation": {
        "spike", "unit_error", "duplicate_load", "pipeline_gap", "ledger_mismatch",
    },
    "dq_duplicates": {"duplicate_load"},
    "dq_late_arrivals": {"late_arrival"},
}

EXCEPTION_QUERIES = {
    "dq_reconciliation": """
        SELECT ACCOUNT_ID, ACTIVITY_DATE AS D, BREAK_KIND AS DETAIL,
               ROUND(REL_DIFF_PCT, 2) AS METRIC
        FROM REVENUE_PULSE.DQ.DQ_RECONCILIATION
    """,
    "dq_duplicates": """
        SELECT ACCOUNT_ID, USAGE_DATE AS D, DUPLICATE_KIND || ':' || PRODUCT AS DETAIL,
               ROW_COUNT AS METRIC
        FROM REVENUE_PULSE.DQ.DQ_DUPLICATES
    """,
    "dq_late_arrivals": """
        SELECT ACCOUNT_ID, USAGE_DATE AS D, 'late' AS DETAIL, LAG_DAYS AS METRIC
        FROM REVENUE_PULSE.DQ.DQ_LATE_ARRIVALS
    """,
}


def main() -> int:
    try:
        conn = connect(schema=SCHEMA)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT LABEL_ID, ANOMALY_TYPE, ACCOUNT_ID, PRODUCT,
                       CAST(START_DATE AS DATE), CAST(END_DATE AS DATE),
                       MAGNITUDE, DESCRIPTION
                FROM REVENUE_PULSE.RAW.ANOMALY_LABELS
                ORDER BY LABEL_ID
                """
            )
            labels = cur.fetchall()

            # (account_id, date) -> labels covering that day. A gap anomaly
            # spans two days, so one label can own several account-days.
            by_day: dict[tuple[str, date], list[tuple]] = defaultdict(list)
            for row in labels:
                _, _, account, _, d0, d1, _, _ = row
                for offset in range((d1 - d0).days + 1):
                    by_day[(account, d0 + timedelta(days=offset))].append(row)

            matched_labels: set[str] = set()
            caught_by: dict[str, set[str]] = defaultdict(set)

            for model, query in EXCEPTION_QUERIES.items():
                cur.execute(query)
                rows = cur.fetchall()
                print(f"\n{'=' * 78}\n{model}: {len(rows)} exception row(s)\n{'=' * 78}")
                print(f"  {'ACCOUNT':10} {'DATE':12} {'DETAIL':26} {'METRIC':>9}  VERDICT")
                print("  " + "-" * 74)
                unmatched = 0
                for account, day, detail, metric in sorted(rows, key=lambda r: (r[0], r[1])):
                    hits = by_day.get((account, day), [])
                    if hits:
                        ids = ",".join(h[0] for h in hits)
                        kinds = ",".join(h[1] for h in hits)
                        verdict = f"planted anomaly {ids} ({kinds})"
                        for h in hits:
                            matched_labels.add(h[0])
                            caught_by[h[0]].add(model)
                    else:
                        verdict = "*** NO LABEL - investigate as a bug ***"
                        unmatched += 1
                    print(
                        f"  {account:10} {str(day):12} {detail:26} "
                        f"{float(metric):>9.2f}  {verdict}"
                    )
                print(f"\n  matched to a planted anomaly: {len(rows) - unmatched}/{len(rows)}")
                if unmatched:
                    print(f"  UNEXPLAINED ROWS: {unmatched} - these are bugs, not anomalies")

            print(f"\n{'=' * 78}\nCoverage: which planted anomalies did the DQ layer catch?\n{'=' * 78}")
            print(f"  {'LABEL':8} {'TYPE':16} {'ACCOUNT':10} {'CAUGHT BY':34} STATUS")
            print("  " + "-" * 74)
            missed_by_design = 0
            missed_unexpected = 0
            for label_id, kind, account, product, d0, d1, mag, desc in labels:
                catchers = sorted(caught_by.get(label_id, []))
                if catchers:
                    status = "caught"
                    shown = ", ".join(catchers)
                else:
                    shown = "-"
                    expected = any(kind in types for types in EXPECTED_CATCHER.values())
                    if expected:
                        status = "*** MISSED - expected to be caught ***"
                        missed_unexpected += 1
                    else:
                        status = "not covered by design"
                        missed_by_design += 1
                print(f"  {label_id:8} {kind:16} {account:10} {shown:34} {status}")

            total = len(labels)
            caught = len(matched_labels)
            print(f"\n  caught {caught}/{total} planted anomalies")
            if missed_unexpected:
                print(f"  {missed_unexpected} miss(es) NOT explained by design - investigate")
            if missed_by_design:
                print(f"  {missed_by_design} not covered by any DQ model by design")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
