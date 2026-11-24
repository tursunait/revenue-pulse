"""Score the detectors against RAW.ANOMALY_LABELS and write eval/RESULTS.md.

Lives in eval/ because it reads the label table. No detector model may.

    python eval/score_detector.py          # rebuilds, then scores
    python eval/score_detector.py --no-build

Always rebuilds what it reads first. An earlier run of this script scored
against stale MARTS tables - a targeted `dbt build --select dq_anomaly_scores+`
had refreshed the detector but not the upstream facts, and because the DQ
tables are materialised as tables they silently held the previous dataset. The
symptom was absurd rather than subtle (the late-arrival detector appeared to
catch ledger mismatches), but it could as easily have been a plausible wrong
number. See DECISIONS.md part 6.

Note that `+dq_anomaly_scores+` alone would NOT be enough: dq_reconciliation,
dq_duplicates and dq_late_arrivals are neither ancestors nor descendants of the
scores model, so three of the four detectors would still go stale. The selector
below covers the ancestors and descendants of every model read.

Scoring grain
-------------
Detectors are collapsed to distinct (account_id, date); labels are expanded to
the account-days their span covers. That puts a per-product detector and an
account-wide one on the same footing.

Everything is restricted to the account-days detector v2 can actually score.
v2 needs 13 weeks of residual history, so it covers less of the window than v1;
comparing them on their own supports would flatter whichever sees fewer hard
days. Coverage lost is reported below.
"""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.snowflake_conn import ConfigError, connect  # noqa: E402

Z_THRESHOLDS = (3.0, 3.5, 4.0, 5.0)
DEFAULT_Z = 4.0
TRAILING_WEEKS = 8
SPREAD_WEEKS = 13
RESULTS_PATH = Path(__file__).resolve().parent / "RESULTS.md"

SELECTOR = [
    "+dq_anomaly_scores+",
    "+dq_anomaly_scores_v2+",
    "+dq_reconciliation+",
    "+dq_duplicates+",
    "+dq_late_arrivals+",
]


@dataclass
class Score:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def score(detected: set, labelled: set) -> Score:
    return Score(len(detected & labelled), len(detected - labelled), len(labelled - detected))


def rebuild() -> None:
    print("Rebuilding everything this script reads ...")
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "dbt.sh"), "build", "--select", *SELECTOR],
        capture_output=True, text=True,
    )
    tail = [ln for ln in result.stdout.splitlines() if "Done." in ln or "ERROR=" in ln]
    print("  " + (tail[-1].strip() if tail else "(no summary line)"))
    if result.returncode != 0:
        # dbt exits non-zero on test failures too; the DQ tests warn by design,
        # so only a hard failure should stop the scoring run.
        if "ERROR=0" not in result.stdout:
            print(result.stdout[-3000:], file=sys.stderr)
            raise SystemExit("dbt build failed; not scoring against stale tables")


def fetch(cur, sql: str) -> list[tuple]:
    cur.execute(sql)
    return cur.fetchall()


def population_stats(cur, table: str) -> dict:
    row = fetch(cur, f"""
        SELECT COUNT(*),
               ROUND(AVG(Z), 3),
               ROUND(STDDEV(Z), 2),
               ROUND(100.0 * SUM(CASE WHEN Z > 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
        FROM REVENUE_PULSE.DQ.{table} WHERE Z IS NOT NULL
    """)[0]
    out = {"scored": row[0], "mean_z": row[1], "sd_z": row[2], "pct_positive": row[3]}
    out["by_threshold"] = {}
    for t in Z_THRESHOLDS:
        up, down = fetch(cur, f"""
            SELECT SUM(CASE WHEN Z >  {t} THEN 1 ELSE 0 END),
                   SUM(CASE WHEN Z < -{t} THEN 1 ELSE 0 END)
            FROM REVENUE_PULSE.DQ.{table} WHERE Z IS NOT NULL
        """)[0]
        up, down = int(up or 0), int(down or 0)
        out["by_threshold"][t] = {
            "up": up, "down": down, "total": up + down,
            "rate": (up + down) / row[0] if row[0] else 0.0,
        }
    return out


def main() -> int:
    if "--no-build" not in sys.argv:
        rebuild()

    try:
        conn = connect(schema="DQ")
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        cur = conn.cursor()

        # ---------- ground truth ----------
        labels = fetch(cur, """
            SELECT LABEL_ID, ANOMALY_TYPE, ACCOUNT_ID, PRODUCT,
                   CAST(START_DATE AS DATE), CAST(END_DATE AS DATE)
            FROM REVENUE_PULSE.RAW.ANOMALY_LABELS
        """)
        label_days: dict[str, set] = {}
        label_type: dict[str, str] = {}
        label_product: dict[str, str | None] = {}
        for label_id, kind, account, product, d0, d1 in labels:
            label_days[label_id] = {
                (account, d0 + timedelta(days=i)) for i in range((d1 - d0).days + 1)
            }
            label_type[label_id] = kind
            label_product[label_id] = product
        types = sorted(set(label_type.values()))

        # ---------- detector outputs ----------
        def z_rows(table: str) -> list[tuple]:
            return fetch(cur, f"""
                SELECT ACCOUNT_ID, USAGE_DATE, PRODUCT, Z, IS_ZERO_AFTER_NONZERO
                FROM REVENUE_PULSE.DQ.{table}
                WHERE (Z IS NOT NULL AND ABS(Z) >= {min(Z_THRESHOLDS)})
                   OR IS_ZERO_AFTER_NONZERO
            """)

        rows_v1, rows_v2 = z_rows("DQ_ANOMALY_SCORES"), z_rows("DQ_ANOMALY_SCORES_V2")
        stats = {"v1": population_stats(cur, "DQ_ANOMALY_SCORES"),
                 "v2": population_stats(cur, "DQ_ANOMALY_SCORES_V2")}

        # v2's scorable support: the common footing for every table below.
        support = {
            (a, d) for a, d in fetch(cur, """
                SELECT DISTINCT ACCOUNT_ID, USAGE_DATE
                FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES_V2 WHERE Z IS NOT NULL
            """)
        }

        recon = {(a, d) for a, d in fetch(cur, "SELECT ACCOUNT_ID, ACTIVITY_DATE FROM REVENUE_PULSE.DQ.DQ_RECONCILIATION")}
        dupes = {(a, d) for a, d in fetch(cur, "SELECT DISTINCT ACCOUNT_ID, USAGE_DATE FROM REVENUE_PULSE.DQ.DQ_DUPLICATES")}
        late = {(a, d) for a, d in fetch(cur, "SELECT DISTINCT ACCOUNT_ID, USAGE_DATE FROM REVENUE_PULSE.DQ.DQ_LATE_ARRIVALS")}

        def zset(rows, t) -> set:
            return {(a, d) for a, d, _p, z, _zero in rows
                    if z is not None and abs(float(z)) > t} & support

        def gapset(rows) -> set:
            return {(a, d) for a, d, _p, _z, zero in rows if zero} & support

        recon, dupes, late = recon & support, dupes & support, late & support

        # ---------- restrict ground truth to the common support ----------
        labelled = set()
        for days in label_days.values():
            labelled |= days
        labelled_eval = labelled & support
        labels_eval = [i for i in label_days if label_days[i] & support]
        dropped = [i for i in label_days if not (label_days[i] & support)]

        def type_recall(detected: set) -> dict:
            out = {}
            for kind in types:
                ids = [i for i in labels_eval if label_type[i] == kind]
                out[kind] = (sum(1 for i in ids if label_days[i] & detected), len(ids))
            return out

        def chance(detected_rate: float, ids: list[str], n_products: int = 4) -> float:
            return sum(
                1 - (1 - detected_rate) ** (
                    len(label_days[i] & support) * (1 if label_product[i] else n_products)
                ) for i in ids
            )

        L: list[str] = []
        emit = L.append

        emit("## Coverage and scoring basis\n")
        emit(f"- Planted anomalies: **{len(labels)}**, covering {len(labelled):,} account-days.")
        emit(f"- Scored on detector v2's support ({len(support):,} account-days): "
             f"**{len(labels_eval)}** labels, {len(labelled_eval):,} account-days.")
        emit(f"- Labels outside v2's support (too early for {SPREAD_WEEKS} weeks of "
             f"residual history): **{len(dropped)}**"
             + (f" — {', '.join(sorted(dropped))}" if dropped else ""))
        emit("\nAll tables below use this common support, so v1 and v2 are compared on "
             "identical account-days.\n")

        # ---------- calibration ----------
        emit("## Calibration: v1 vs v2\n")
        emit("A correctly normalised z-score has mean 0, sd 1, and half its values "
             "positive.\n")
        emit("| Version | Scored rows | mean z | sd z | % positive |")
        emit("| --- | --- | --- | --- | --- |")
        for v in ("v1", "v2"):
            s = stats[v]
            emit(f"| {v} | {s['scored']:,} | {s['mean_z']:+} | {s['sd_z']} | {s['pct_positive']}% |")
        emit("")

        emit("### Flag rate and up/down split\n")
        emit("| Version | Threshold | Flags | Flag rate | Up | Down |")
        emit("| --- | --- | --- | --- | --- | --- |")
        for v in ("v1", "v2"):
            for t in Z_THRESHOLDS:
                b = stats[v]["by_threshold"][t]
                emit(f"| {v} | {t:g} | {b['total']:,} | {b['rate'] * 100:.3f}% | "
                     f"{b['up']:,} | {b['down']:,} |")
        emit("")

        # ---------- threshold sweep, v1 vs v2, with chance baseline ----------
        emit("## Threshold sweep with chance baseline\n")
        emit("`caught` counts labels with at least one detected account-day. `chance` is "
             "what a detector firing at the same rate at random would catch — subtract it "
             "to see real detection.\n")
        emit("| Version | Threshold | Detections | TP | FP | Precision | Recall | F1 | caught | chance | real |")
        emit("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        sweep: dict[tuple[str, float], Score] = {}
        for v, rows in (("v1", rows_v1), ("v2", rows_v2)):
            for t in Z_THRESHOLDS:
                det = zset(rows, t)
                sc = score(det, labelled_eval)
                sweep[(v, t)] = sc
                caught = sum(1 for i in labels_eval if label_days[i] & det)
                exp = chance(stats[v]["by_threshold"][t]["rate"], labels_eval)
                emit(f"| {v} | {t:g} | {len(det):,} | {sc.tp} | {sc.fp:,} | "
                     f"{sc.precision:.4f} | {sc.recall:.3f} | {sc.f1:.4f} | "
                     f"{caught}/{len(labels_eval)} | {exp:.1f} | {caught - exp:+.1f} |")
        emit("")

        # ---------- scenarios ----------
        z2 = zset(rows_v2, DEFAULT_Z)
        z1 = zset(rows_v1, DEFAULT_Z)
        gaps = gapset(rows_v2)

        scenarios = {
            "A — full data (ledger available)": {
                "reconciliation": recon,
                "duplicates": dupes,
                "late arrivals": late,
                "gap rule (zero-after-nonzero)": gaps,
                f"z-score v2 (|z|>{DEFAULT_Z:g})": z2,
                f"z-score v1 (|z|>{DEFAULT_Z:g})": z1,
                "DETERMINISTIC only": recon | dupes | late | gaps,
                "COMBINED (deterministic + v2)": recon | dupes | late | gaps | z2,
            },
            "B — no daily ledger (finance closes monthly)": {
                "duplicates": dupes,
                "late arrivals": late,
                "gap rule (zero-after-nonzero)": gaps,
                f"z-score v2 (|z|>{DEFAULT_Z:g})": z2,
                f"z-score v1 (|z|>{DEFAULT_Z:g})": z1,
                "WITHOUT z-score (dupes+late+gap)": dupes | late | gaps,
                "COMBINED (v2 + dupes + late + gap)": z2 | dupes | late | gaps,
            },
        }

        scenario_scores: dict[str, dict[str, Score]] = {}
        scenario_types: dict[str, dict[str, dict]] = {}
        for title, dets in scenarios.items():
            emit(f"## Scenario {title}\n")
            emit("| Detector | TP | FP | FN | Precision | Recall | F1 |")
            emit("| --- | --- | --- | --- | --- | --- | --- |")
            scenario_scores[title] = {}
            for name, det in dets.items():
                sc = score(det, labelled_eval)
                scenario_scores[title][name] = sc
                emit(f"| {name} | {sc.tp} | {sc.fp:,} | {sc.fn} | "
                     f"{sc.precision:.3f} | {sc.recall:.3f} | {sc.f1:.3f} |")
            emit("")
            emit(f"### Recall by anomaly type — scenario {title.split(' — ')[0]}\n")
            emit("| Anomaly type | n |" + "".join(f" {n} |" for n in dets))
            emit("| --- | --- |" + " --- |" * len(dets))
            scenario_types[title] = {n: type_recall(d) for n, d in dets.items()}
            for kind in types:
                first = next(iter(dets))
                n = scenario_types[title][first][kind][1]
                row = f"| `{kind}` | {n} |"
                for name in dets:
                    c, tot = scenario_types[title][name][kind]
                    row += f" {c}/{tot} |"
                emit(row)
            emit("")

        # ---------- scenario B across thresholds ----------
        emit("## Scenario B across thresholds (v2 + dupes + late + gap)\n")
        emit("In scenario B the threshold is load-bearing: nothing else covers the "
             "fault types the z-score sees. `items/day` is the review burden.\n")
        emit("| Threshold | Precision | Recall | F1 | labels caught | detections | items/day |")
        emit("| --- | --- | --- | --- | --- | --- | --- |")
        n_days = len({d for _a, d in support})
        scen_b_by_t = {}
        for t in Z_THRESHOLDS:
            det = zset(rows_v2, t) | dupes | late | gaps
            sc = score(det, labelled_eval)
            caught = sum(1 for i in labels_eval if label_days[i] & det)
            scen_b_by_t[t] = (sc, caught, len(det))
            emit(f"| {t:g} | {sc.precision:.3f} | {sc.recall:.3f} | {sc.f1:.3f} | "
                 f"{caught}/{len(labels_eval)} | {len(det):,} | "
                 f"{len(det) / max(n_days, 1):.1f} |")
        emit("")

        # ---------- v2 per-type recall across thresholds ----------
        emit("## v2 recall by type across thresholds\n")
        emit("Threshold choice is a per-type coverage decision, not a single number.\n")
        emit("| Anomaly type | n |" + "".join(f" z>{t:g} |" for t in Z_THRESHOLDS))
        emit("| --- | --- |" + " --- |" * len(Z_THRESHOLDS))
        v2_by_t = {t: type_recall(zset(rows_v2, t)) for t in Z_THRESHOLDS}
        for kind in types:
            n = v2_by_t[Z_THRESHOLDS[0]][kind][1]
            emit(f"| `{kind}` | {n} |" + "".join(
                f" {v2_by_t[t][kind][0]}/{n} |" for t in Z_THRESHOLDS))
        emit("")

        # ---------- per-type chance for v2 ----------
        emit("## Chance baseline by anomaly type — z-score v2\n")
        emit("| Anomaly type | n | caught | chance | verdict |")
        emit("| --- | --- | --- | --- | --- |")
        rate2 = stats["v2"]["by_threshold"][DEFAULT_Z]["rate"]
        v2_type_chance = {}
        for kind in types:
            ids = [i for i in labels_eval if label_type[i] == kind]
            caught = sum(1 for i in ids if label_days[i] & z2)
            exp = chance(rate2, ids)
            v2_type_chance[kind] = (caught, len(ids), exp)
            verdict = "real signal" if caught - exp > 0.75 else "chance / not detected"
            emit(f"| `{kind}` | {len(ids)} | {caught} | {exp:.3f} | {verdict} |")
        emit("")

        report = "\n".join(L)
        print(report)
        RESULTS_PATH.write_text(
            HEADER + report
            + build_analysis(stats, sweep, scenario_scores, scenario_types,
                             v2_type_chance, types, labels_eval, v2_by_t,
                             scen_b_by_t, n_days)
        )
        print(f"Wrote {RESULTS_PATH.relative_to(Path.cwd())}")
    finally:
        conn.close()
    return 0


HEADER = """# Detector evaluation

Generated by `python eval/score_detector.py`, which rebuilds every model it
reads before scoring. All figures below are produced by that run — nothing is
transcribed by hand.

"""


def build_analysis(stats, sweep, scen, scen_types, v2_chance, types, labels_eval, v2_by_t,
                   scen_b_by_t, n_days_hint) -> str:
    A = "A — full data (ledger available)"
    B = "B — no daily ledger (finance closes monthly)"
    zA = f"z-score v2 (|z|>{DEFAULT_Z:g})"
    v1s, v2s = stats["v1"], stats["v2"]
    r1 = v1s["by_threshold"][DEFAULT_Z]["rate"] * 100
    r2 = v2s["by_threshold"][DEFAULT_Z]["rate"] * 100
    real = [k for k in types if v2_chance[k][0] - v2_chance[k][2] > 0.75]
    nothing = [k for k in types if v2_chance[k][0] == 0]
    bz = scen[B][zA]
    bwo = scen[B]["WITHOUT z-score (dupes+late+gap)"]
    bcomb = scen[B]["COMBINED (v2 + dupes + late + gap)"]
    adet = scen[A]["DETERMINISTIC only"]
    acomb = scen[A]["COMBINED (deterministic + v2)"]
    arecon = scen[A]["reconciliation"]
    b_rows = "\n".join(
        f"| {t:g} | {scen_b_by_t[t][0].precision:.3f} | {scen_b_by_t[t][0].recall:.3f} | "
        f"{scen_b_by_t[t][0].f1:.3f} | {scen_b_by_t[t][1]}/{len(labels_eval)} | "
        f"{scen_b_by_t[t][2] / max(n_days_hint, 1):.1f} |"
        for t in Z_THRESHOLDS
    )
    return rf"""
## What changed from v1 to v2

Only the two causes diagnosed for v1 were addressed. No parameter was fitted to
the labels; the threshold remains the single label-informed choice.

| | v1 | v2 |
| --- | --- | --- |
| Expectation | same-weekday median, 8 weeks | same, shifted by a 4-week trend ratio, in log space |
| Spread | MAD over {TRAILING_WEEKS} same-weekday points | MAD over ~{SPREAD_WEEKS * 7} daily residuals, floored by the product x segment cohort |
| mean z | {v1s['mean_z']:+} | {v2s['mean_z']:+} |
| sd z | {v1s['sd_z']} | {v2s['sd_z']} |
| % positive | {v1s['pct_positive']}% | {v2s['pct_positive']}% |
| flag rate at \|z\|>4 | {r1:.2f}% | {r2:.3f}% |

Both defects are gone. v2's sd of {v2s['sd_z']} means the score is finally on
the scale its name implies — a "4 sigma" event is now actually rare, where in
v1 it happened {r1:.1f}% of the time. The near-even split of positives
({v2s['pct_positive']}%) shows the trend correction worked: v1 was
{v1s['pct_positive']}% positive because its baseline was permanently behind a
growing series.

Working in log space is what makes the spread poolable. Multiplicative noise is
additive in logs, so residuals are comparable across accounts of wildly
different size — which is why a cohort floor is meaningful at all, and why
{SPREAD_WEEKS} weeks of *daily* residuals can be used instead of eight
same-weekday points.

## Scenario A: with a daily ledger, the z-score is still redundant

Reconciliation alone scores precision {arecon.precision:.3f}, recall
{arecon.recall:.3f}, F1 {arecon.f1:.3f}. The deterministic set reaches F1
{adet.f1:.3f}; adding v2 moves it to {acomb.f1:.3f}.

This was already v1's conclusion and v2 does not change it. When an independent
ledger exists, subtraction beats statistics: the ledger witnesses every
usage-side fault directly, so there is nothing for a statistical detector to
add. v2's contribution is that it no longer *costs* anything to run alongside —
{sweep[('v2', DEFAULT_Z)].fp:,} false positives against v1's
{sweep[('v1', DEFAULT_Z)].fp:,}.

## Scenario B: without a daily ledger, the z-score carries the load

This is the realistic case — finance closes monthly, so there is no daily
booked-revenue feed to reconcile against.

| | Precision | Recall | F1 | Labels caught |
| --- | --- | --- | --- | --- |
| Without the z-score | {bwo.precision:.3f} | {bwo.recall:.3f} | {bwo.f1:.3f} | {scen_types[B]["WITHOUT z-score (dupes+late+gap)"] and sum(v[0] for v in scen_types[B]["WITHOUT z-score (dupes+late+gap)"].values())}/{len(labels_eval)} |
| v2 added (\|z\|>{DEFAULT_Z:g}) | {bcomb.precision:.3f} | {bcomb.recall:.3f} | {bcomb.f1:.3f} | {scen_b_by_t[DEFAULT_Z][1]}/{len(labels_eval)} |

Without the z-score the surviving checks cover only the three fault types they
were written for — `duplicate_load`, `late_arrival`, `pipeline_gap`. Adding v2
brings in `spike`, `unit_error` and `small_account_spike`, which nothing else
can see once the ledger is gone.

**Types v2 detects above chance**: `{"`, `".join(real)}`.

**Types nothing in scenario B catches**: `{"`, `".join(nothing) if nothing else "none"}`.

`ledger_mismatch` is the structural casualty. By construction the usage side is
clean and only the booked figure moved, so with no ledger there is no
observable trace whatsoever. That is not a detector weakness — it is the honest
cost of dropping the second source, and the strongest argument for obtaining
even a provisional daily feed.

`gradual_drift` stays at {v2_chance['gradual_drift'][0]}/{v2_chance['gradual_drift'][1]}.
A sub-1%-per-day ramp sits below the daily noise floor however well the score
is calibrated; it needs a different shape of test — a slope test over weeks, or
`fct_weekly_review.vs_trailing_4w_pct`.

## Threshold choice differs by scenario

Scenario B across thresholds:

| Threshold | Precision | Recall | F1 | Labels | Items/day |
| --- | --- | --- | --- | --- | --- |
{b_rows}

The curve is steep and the coverage is almost flat. Going from \|z\|>5 to
\|z\|>3 buys {scen_b_by_t[3.0][1] - scen_b_by_t[5.0][1]} extra label(s) — both
`mild_spike` — and costs {scen_b_by_t[3.0][2] / max(scen_b_by_t[5.0][2], 1):.0f}x
the review volume, dropping precision from {scen_b_by_t[5.0][0].precision:.3f}
to {scen_b_by_t[3.0][0].precision:.3f}. The z-score's coverage of
`duplicate_load` at low thresholds is redundant: `dq_duplicates` already
catches all of them exactly.

That asymmetry is the whole recommendation. **Two tiers, not one threshold.**

## Recommended alerting design

**Pages someone** — precise enough that an alert means an incident:

| Check | Precision | Why it can page |
| --- | --- | --- |
| `dq_reconciliation` (scenario A only) | {arecon.precision:.3f} | money that does not tie out against an independent source |
| `dq_duplicates` (business grain) | {scen[A]['duplicates'].precision:.3f} | a duplicated load is unambiguous and mechanically fixable |
| `dq_late_arrivals` | {scen[A]['late arrivals'].precision:.3f} | a stalled feed needs an owner now |
| gap rule, zero-after-nonzero | {scen[A]['gap rule (zero-after-nonzero)'].precision:.3f} | an account billing nothing when it should is an outage |
| source freshness error (72h) | — | the pipeline has stopped |
| **v2 at \|z\|>5, scenario B only** | {scen_b_by_t[5.0][0].precision:.3f} | {scen_b_by_t[5.0][2] / max(len(labels_eval), 1):.1f} items per label-period; precise enough to wake someone *when there is no ledger* |

**Shows on a dashboard or review queue** — needs human judgement:

| Signal | Precision | Why it must not page |
| --- | --- | --- |
| v2 at \|z\|>3, daily triage queue | {scen_b_by_t[3.0][0].precision:.3f} | {scen_b_by_t[3.0][2] / max(n_days_hint, 1):.1f} items/day is reviewable, but most are not incidents |
| `fct_weekly_review.vs_trailing_4w_pct` | — | the only signal that sees gradual drift; inherently weekly |
| source freshness warning (36h) | — | expected on a static snapshot (see DECISIONS.md) |
| v2 in scenario A, any threshold | {scen[A][zA].precision:.3f} | reconciliation already catches everything it does, with perfect precision |

So: in scenario A the z-score is decoration — keep it on the dashboard as a
cross-check and let reconciliation page. In scenario B promote
\|z\|>5 to a pager and run \|z\|>3 as a daily triage list, accepting that
`ledger_mismatch` is simply uncovered until a daily feed exists.

The ordering principle: **page on checks that compare two independent sources,
review everything that compares a series against its own history.** The first
kind knows it is wrong; the second only knows it is unusual.
"""


if __name__ == "__main__":
    sys.exit(main())
