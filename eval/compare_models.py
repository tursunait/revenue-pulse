"""Compare models on the same week: verifier pass rate, latency, cost.

    python eval/compare_models.py 2026-10-05
    python eval/compare_models.py 2026-10-05 --runs 3

Generates in memory and does NOT write DQ.AI_SUMMARY, so the stored summary is
untouched. Each run costs real money - three models at one run each is a
fraction of a cent here, but the count is printed so it is never a surprise.

Models compared: claude-opus-5-5, claude-sonnet-5-5 and claude-haiku-5-5.
Haiku 4.5 is kept in MODEL_CONFIG for reference but is a generation behind;
it is the one model here that rejects output_config.effort and server-side
fallbacks, which is why MODEL_CONFIG in scripts/generate_summary.py carries a
per-model request shape rather than one set of keyword arguments.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from common.quiet_streamlit import silence  # noqa: E402
silence()
sys.path.insert(0, str(REPO_ROOT / "app"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from claims import check_claims  # noqa: E402
from common.snowflake_conn import connect  # noqa: E402
from verifier import verify  # noqa: E402

import generate_summary as gen  # noqa: E402

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("week_start")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    week = date.fromisoformat(args.week_start)

    conn = connect(schema="DQ")
    try:
        payload = gen.build_payload(conn, week)
        print(f"week {week} · payload {len(str(payload))} bytes · "
              f"{len(MODELS)} models x {args.runs} run(s) = "
              f"{len(MODELS) * args.runs} API call(s)\n")

        results: dict[str, list[dict]] = {m: [] for m in MODELS}
        texts: dict[str, str] = {}

        for model in MODELS:
            for run in range(args.runs):
                try:
                    summary, label, usage = gen.summarise_anthropic(payload, model)
                except SystemExit as exc:
                    print(f"  {model}: FAILED - {exc}")
                    continue
                numeric = verify(summary, payload)
                claims = check_claims(summary, payload)
                errors = [c for c in claims if c.severity == "error"]
                results[model].append({
                    "ready": numeric.ok and not errors,
                    "figures": (numeric.matched_numbers, numeric.total_numbers),
                    "ids": (numeric.matched_identifiers, numeric.total_identifiers),
                    "claim_errors": errors,
                    "numeric_fails": [b.unmatched for b in numeric.bullets if not b.ok],
                    "bullets": len([b for b in numeric.bullets if b.text.startswith("-")]),
                    **usage,
                })
                if run == 0:
                    texts[model] = summary
                print(f"  {model} run {run + 1}: "
                      f"{'READY' if numeric.ok and not errors else 'BLOCKED'} · "
                      f"{usage['latency_s']}s · ${usage['cost_usd']:.6f}")
    finally:
        conn.close()

    print("\n" + "=" * 94)
    print(f"{'model':20} {'ready':>7} {'figures':>9} {'ids':>7} {'claims':>7} "
          f"{'bullets':>8} {'latency':>9} {'tok in/out':>14} {'cost':>10}")
    print("-" * 94)
    for model in MODELS:
        runs = results[model]
        if not runs:
            print(f"{model:20} {'n/a':>7}")
            continue
        ready = sum(1 for r in runs if r["ready"])
        fig = sum(r["figures"][0] for r in runs), sum(r["figures"][1] for r in runs)
        ids = sum(r["ids"][0] for r in runs), sum(r["ids"][1] for r in runs)
        claim_errs = sum(len(r["claim_errors"]) for r in runs)
        lat = statistics.mean(r["latency_s"] for r in runs)
        tin = statistics.mean(r["input_tokens"] for r in runs)
        tout = statistics.mean(r["output_tokens"] for r in runs)
        cost = statistics.mean(r["cost_usd"] for r in runs)
        bullets = statistics.mean(r["bullets"] for r in runs)
        print(f"{model:20} {ready}/{len(runs):<5} {fig[0]}/{fig[1]:<7} "
              f"{ids[0]}/{ids[1]:<5} {claim_errs:>7} {bullets:>8.1f} "
              f"{lat:>8.2f}s {int(tin):>6,}/{int(tout):<6,} ${cost:>8.6f}")

    print("\nFailures in detail:")
    any_fail = False
    for model in MODELS:
        for i, r in enumerate(results[model], start=1):
            if r["ready"]:
                continue
            any_fail = True
            print(f"  {model} run {i}:")
            for unmatched in r["numeric_fails"]:
                print(f"     figure not in aggregates: {unmatched}")
            for issue in r["claim_errors"]:
                print(f"     claim [{issue.check}] b{issue.bullet_index}: {issue.detail}")
    if not any_fail:
        print("  none")

    print("\nFirst run of each model:")
    for model in MODELS:
        if model in texts:
            print(f"\n--- {model} ---")
            print(texts[model])
    return 0


if __name__ == "__main__":
    sys.exit(main())
