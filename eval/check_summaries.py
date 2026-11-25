"""Run the numeric verifier and the claim checks over every stored summary.

    python eval/check_summaries.py

In eval/ because it is a review tool, though it reads no labels - the app runs
the same two checks live on the page.
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
from common.snowflake_conn import connect  # noqa: E402
from verifier import verify  # noqa: E402


def main() -> int:
    conn = connect(schema="DQ")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT TO_VARCHAR(WEEK_START,'YYYY-MM-DD'), MODEL, INPUT_JSON, SUMMARY
            FROM REVENUE_PULSE.DQ.AI_SUMMARY ORDER BY WEEK_START
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    all_ready = True
    for week, model, payload, summary in rows:
        numeric = verify(summary, payload)
        claims = check_claims(summary, payload)
        errors = [c for c in claims if c.severity == "error"]
        warnings = [c for c in claims if c.severity != "error"]
        ready = numeric.ok and not errors
        all_ready &= ready

        print(f"=== {week}  [{model}]")
        print(f"    figures     {numeric.matched_numbers}/{numeric.total_numbers}"
              f"   identifiers {numeric.matched_identifiers}/{numeric.total_identifiers}")
        print(f"    claims      {len(errors)} error(s), {len(warnings)} warning(s)")
        print(f"    verdict     {'READY TO SEND' if ready else 'BLOCKED'}")
        for bullet in numeric.bullets:
            if not bullet.ok:
                print(f"      figure FLAG b{numeric.bullets.index(bullet) + 1}: "
                      f"{bullet.unmatched}")
        for issue in claims:
            tag = "ERROR" if issue.severity == "error" else "warn "
            print(f"      claim {tag} [{issue.check}] b{issue.bullet_index}: "
                  f"{issue.detail[:120]}")
        print()

    print("all summaries ready to send" if all_ready
          else "at least one summary is blocked")
    return 0 if all_ready else 1


if __name__ == "__main__":
    sys.exit(main())
