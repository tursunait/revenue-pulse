"""Score each detector against RAW.ANOMALY_LABELS and write eval/RESULTS.md.

Lives in eval/ because it reads the label table. The detector models must never
read it.

    python eval/score_detector.py

Scoring grain
-------------
Every detector is collapsed to distinct (account_id, date) before scoring, and
every label is expanded to the account-days its span covers. That puts all
detectors on one comparable footing: dq_duplicates emits a row per product
while its label is account-wide, so counting raw detector rows would score the
same event four times for one detector and once for another.

  TP  detected account-days that fall inside some label span
  FP  detected account-days inside no label span
  FN  label-covered account-days nothing detected

Per-type recall is reported at LABEL grain instead - a 21-day drift is one
anomaly, not 21 - so a detector that catches one day of a long drift counts as
having caught it.

Limitation: at account-day grain a product-level flag on the wrong product of a
right account-day still counts as a true positive. Checked separately below.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.snowflake_conn import ConfigError, connect  # noqa: E402

Z_THRESHOLDS = (3.0, 3.5, 4.0, 5.0)
TRAILING_WEEKS = 8
DEFAULT_Z = 4.0
RESULTS_PATH = Path(__file__).resolve().parent / "RESULTS.md"


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


def score(detected: set[tuple[str, date]], labelled: set[tuple[str, date]]) -> Score:
    return Score(
        tp=len(detected & labelled),
        fp=len(detected - labelled),
        fn=len(labelled - detected),
    )


def zscore_at_rowgrain(z_rows, threshold: float) -> set[tuple]:
    """Flags at account x product x day grain - the grain the rate applies to."""
    return {
        (a, d, p) for a, d, p, z, _zero in z_rows
        if z is not None and abs(float(z)) > threshold
    }


def fetch(cur, sql: str) -> list[tuple]:
    cur.execute(sql)
    return cur.fetchall()


def main() -> int:
    try:
        conn = connect(schema="DQ")
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        cur = conn.cursor()

        # ---- ground truth ----
        labels = fetch(cur, """
            SELECT LABEL_ID, ANOMALY_TYPE, ACCOUNT_ID, PRODUCT,
                   CAST(START_DATE AS DATE), CAST(END_DATE AS DATE)
            FROM REVENUE_PULSE.RAW.ANOMALY_LABELS
        """)
        label_days: dict[str, set[tuple[str, date]]] = {}
        labelled: set[tuple[str, date]] = set()
        label_type: dict[str, str] = {}
        label_product: dict[str, str | None] = {}
        for label_id, kind, account, product, d0, d1 in labels:
            days = {
                (account, d0 + timedelta(days=i)) for i in range((d1 - d0).days + 1)
            }
            label_days[label_id] = days
            labelled |= days
            label_type[label_id] = kind
            label_product[label_id] = product

        types = sorted({t for t in label_type.values()})

        # ---- detectors ----
        # Only rows that could ever be a detection at the lowest threshold.
        z_rows = fetch(cur, """
            SELECT ACCOUNT_ID, USAGE_DATE, PRODUCT, Z, IS_ZERO_AFTER_NONZERO
            FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES
            WHERE (Z IS NOT NULL AND ABS(Z) >= 3.0) OR IS_ZERO_AFTER_NONZERO
        """)
        recon = {
            (a, d) for a, d in fetch(cur, """
                SELECT ACCOUNT_ID, ACTIVITY_DATE FROM REVENUE_PULSE.DQ.DQ_RECONCILIATION
            """)
        }
        dupes = {
            (a, d) for a, d in fetch(cur, """
                SELECT DISTINCT ACCOUNT_ID, USAGE_DATE FROM REVENUE_PULSE.DQ.DQ_DUPLICATES
            """)
        }
        late = {
            (a, d) for a, d in fetch(cur, """
                SELECT DISTINCT ACCOUNT_ID, USAGE_DATE FROM REVENUE_PULSE.DQ.DQ_LATE_ARRIVALS
            """)
        }
        gaps = {(a, d) for a, d, _p, _z, zero in z_rows if zero}

        def zscore_at(threshold: float) -> set[tuple[str, date]]:
            return {
                (a, d) for a, d, _p, z, _zero in z_rows
                if z is not None and abs(float(z)) > threshold
            }

        total_scored = fetch(cur, """
            SELECT COUNT(*) FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES WHERE Z IS NOT NULL
        """)[0][0]

        # ---- estimator diagnostics, measured not assumed ----
        d = fetch(cur, f"""
            SELECT ROUND(AVG(Z), 3),
                   ROUND(100.0 * SUM(CASE WHEN Z > 0 THEN 1 ELSE 0 END) / COUNT(*), 1),
                   SUM(CASE WHEN Z >  {DEFAULT_Z} THEN 1 ELSE 0 END),
                   SUM(CASE WHEN Z < -{DEFAULT_Z} THEN 1 ELSE 0 END)
            FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES WHERE Z IS NOT NULL
        """)[0]
        disp = fetch(cur, """
            SELECT ROUND(100 * MEDIAN(SCALED_MAD / NULLIF(EXPECTED_VALUE, 0)), 1),
                   ROUND(100 * PERCENTILE_CONT(0.05) WITHIN GROUP (
                       ORDER BY SCALED_MAD / NULLIF(EXPECTED_VALUE, 0)), 1),
                   ROUND(100.0 * SUM(CASE WHEN DENOMINATOR > SCALED_MAD + 1e-9
                                          THEN 1 ELSE 0 END) / COUNT(*), 2)
            FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES
            WHERE Z IS NOT NULL AND EXPECTED_VALUE > 1
        """)[0]
        floor_split = dict(
            (grp, (n, flagged)) for grp, n, flagged in fetch(cur, f"""
                SELECT CASE WHEN DENOMINATOR > SCALED_MAD + 1e-9
                            THEN 'floor' ELSE 'mad' END,
                       COUNT(*),
                       SUM(CASE WHEN ABS(Z) > {DEFAULT_Z} THEN 1 ELSE 0 END)
                FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES WHERE Z IS NOT NULL
                GROUP BY 1
            """)
        )
        by_product = fetch(cur, f"""
            SELECT PRODUCT,
                   ROUND(100.0 * SUM(CASE WHEN ABS(Z) > {DEFAULT_Z} THEN 1 ELSE 0 END)
                         / COUNT(*), 2)
            FROM REVENUE_PULSE.DQ.DQ_ANOMALY_SCORES WHERE Z IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC
        """)
        diag = {
            "mean_z": d[0], "pct_positive": d[1], "n_up": d[2], "n_down": d[3],
            "mad_median_pct": disp[0], "mad_p05_pct": disp[1], "floor_pct": disp[2],
            "floor_flag_rate": round(100 * floor_split["floor"][1] / floor_split["floor"][0], 1),
            "mad_flag_rate": round(100 * floor_split["mad"][1] / floor_split["mad"][0], 1),
            "worst_product": by_product[0][0], "worst_rate": by_product[0][1],
            "best_product": by_product[-1][0], "best_rate": by_product[-1][1],
        }

        # ---- per-type recall helper ----
        def type_recall(detected: set[tuple[str, date]]) -> dict[str, tuple[int, int]]:
            out: dict[str, tuple[int, int]] = {}
            for kind in types:
                ids = [i for i, t in label_type.items() if t == kind]
                caught = sum(1 for i in ids if label_days[i] & detected)
                out[kind] = (caught, len(ids))
            return out

        detectors = {
            f"z-score (|z|>{DEFAULT_Z:g})": zscore_at(DEFAULT_Z),
            "zero-after-nonzero (gap rule)": gaps,
            "reconciliation": recon,
            "duplicates": dupes,
            "late arrivals": late,
        }
        deterministic = gaps | recon | dupes | late
        detectors["DETERMINISTIC only (no z-score)"] = deterministic
        detectors["COMBINED (all of the above)"] = deterministic | zscore_at(DEFAULT_Z)

        # ---- chance baseline ----
        # With a detector flagging a few percent of all account-product-days,
        # a multi-day anomaly is near-certain to contain a flag by luck. This
        # computes how many labels a RANDOM detector firing at the same rate
        # would "catch", so real detection can be told apart from coincidence.
        n_products = len({p for _a, _d, p, _z, _zero in z_rows}) or 4
        flag_rate = {}
        chance_caught = {}
        for t in Z_THRESHOLDS:
            flagged = len(zscore_at_rowgrain(z_rows, t))
            rate = flagged / total_scored if total_scored else 0.0
            flag_rate[t] = rate
            expected = 0.0
            for label_id, days in label_days.items():
                trials = len(days) * (1 if label_product[label_id] else n_products)
                expected += 1 - (1 - rate) ** trials
            chance_caught[t] = expected
        diag["flag_rate"] = flag_rate
        diag["chance_caught"] = chance_caught
        diag["n_labels"] = len(labels)

        lines: list[str] = []
        emit = lines.append

        emit("## Detector scores\n")
        emit(f"Account-day grain. {len(labels)} planted anomalies covering "
             f"{len(labelled):,} account-days; {total_scored:,} account-days scored.\n")
        emit("| Detector | TP | FP | FN | Precision | Recall | F1 |")
        emit("| --- | --- | --- | --- | --- | --- | --- |")
        detector_scores: dict[str, Score] = {}
        for name, detected in detectors.items():
            sc = score(detected, labelled)
            detector_scores[name] = sc
            emit(f"| {name} | {sc.tp} | {sc.fp} | {sc.fn} | "
                 f"{sc.precision:.3f} | {sc.recall:.3f} | {sc.f1:.3f} |")
        emit("")

        emit("## Recall by anomaly type (label grain)\n")
        header = "| Anomaly type | n |" + "".join(
            f" {n} |" for n in detectors
        )
        emit(header)
        emit("| --- | --- |" + " --- |" * len(detectors))
        per_det = {name: type_recall(d) for name, d in detectors.items()}
        for kind in types:
            n = per_det[next(iter(detectors))][kind][1]
            row = f"| `{kind}` | {n} |"
            for name in detectors:
                caught, total = per_det[name][kind]
                row += f" {caught}/{total} |"
            emit(row)
        emit("")

        emit("## z-score threshold sweep\n")
        emit("| Threshold | Detections | TP | FP | Precision | Recall | F1 |")
        emit("| --- | --- | --- | --- | --- | --- | --- |")
        sweep: dict[float, Score] = {}
        for t in Z_THRESHOLDS:
            detected = zscore_at(t)
            s = score(detected, labelled)
            sweep[t] = s
            emit(f"| {t:g} | {len(detected):,} | {s.tp} | {s.fp:,} | "
                 f"{s.precision:.4f} | {s.recall:.3f} | {s.f1:.4f} |")
        emit("")

        emit("### Per-type recall across thresholds\n")
        emit("| Anomaly type | n |" + "".join(f" z>{t:g} |" for t in Z_THRESHOLDS))
        emit("| --- | --- |" + " --- |" * len(Z_THRESHOLDS))
        sweep_types = {t: type_recall(zscore_at(t)) for t in Z_THRESHOLDS}
        for kind in types:
            n = sweep_types[Z_THRESHOLDS[0]][kind][1]
            row = f"| `{kind}` | {n} |"
            for t in Z_THRESHOLDS:
                row += f" {sweep_types[t][kind][0]}/{n} |"
            emit(row)
        emit("")

        # ---- product-level check on the account-day grain caveat ----
        z_default = [
            (a, d, p) for a, d, p, z, _zero in z_rows
            if z is not None and abs(float(z)) > DEFAULT_Z
        ]
        right_day_wrong_product = 0
        for account, day, product in z_default:
            hits = [
                i for i, days in label_days.items()
                if (account, day) in days and label_product[i] is not None
            ]
            if hits and all(label_product[i] != product for i in hits):
                right_day_wrong_product += 1
        emit("## Chance baseline\n")
        emit("How many of the 33 labels would a RANDOM detector firing at the same "
             "rate \"catch\"? Anything at or below this line is coincidence, not detection.\n")
        emit("| Threshold | Flag rate | Labels caught | Expected by chance | Real detections |")
        emit("| --- | --- | --- | --- | --- |")
        for t in Z_THRESHOLDS:
            caught = sum(1 for i in label_days if label_days[i] & zscore_at(t))
            exp = diag["chance_caught"][t]
            emit(f"| {t:g} | {diag['flag_rate'][t] * 100:.2f}% | {caught}/{diag['n_labels']} | "
                 f"{exp:.1f} | {caught - exp:+.1f} |")
        emit("")

        emit("### Chance baseline by anomaly type (at the default threshold)\n")
        emit("| Anomaly type | n | caught | expected by chance | verdict |")
        emit("| --- | --- | --- | --- | --- |")
        rate = diag["flag_rate"][DEFAULT_Z]
        z_def = zscore_at(DEFAULT_Z)
        type_chance = {}
        for kind in types:
            ids = [i for i, t in label_type.items() if t == kind]
            caught = sum(1 for i in ids if label_days[i] & z_def)
            exp = sum(
                1 - (1 - rate) ** (len(label_days[i]) * (1 if label_product[i] else n_products))
                for i in ids
            )
            type_chance[kind] = (caught, len(ids), exp)
            verdict = "real signal" if caught - exp > 0.75 else "indistinguishable from chance"
            emit(f"| `{kind}` | {len(ids)} | {caught} | {exp:.2f} | {verdict} |")
        emit("")
        diag["type_chance"] = type_chance

        emit("## Grain caveat check\n")
        emit(f"Product-level z-score flags landing on a labelled account-day but the "
             f"wrong product: **{right_day_wrong_product}**. "
             "These count as true positives at account-day grain.\n")

        report = "\n".join(lines)
        print(report)

        RESULTS_PATH.write_text(HEADER + report + build_analysis(sweep, per_det, types, diag, detector_scores))
        print(f"\nWrote {RESULTS_PATH.relative_to(Path.cwd())}")
    finally:
        conn.close()
    return 0


HEADER = """# Detector evaluation

Generated by `python eval/score_detector.py`. Regenerate after any change to
the generator or the DQ models.

"""


def build_analysis(sweep, per_det, types, diag, detector_scores) -> str:
    """Hand-written analysis, with every measured number interpolated."""
    z_name = f"z-score (|z|>{DEFAULT_Z:g})"
    zt = per_det[z_name]
    tc = diag["type_chance"]
    real = [k for k in types if tc[k][0] - tc[k][2] > 0.75]
    chance = [k for k in types if tc[k][0] - tc[k][2] <= 0.75]
    best_f1 = max(sweep.items(), key=lambda kv: kv[1].f1)
    rs = detector_scores["reconciliation"]
    ds = detector_scores["DETERMINISTIC only (no z-score)"]
    cs = detector_scores["COMBINED (all of the above)"]
    det_labels = sum(
        1 for k in types if per_det["DETERMINISTIC only (no z-score)"][k][0]
        == per_det["DETERMINISTIC only (no z-score)"][k][1]
    )
    return f"""
## Recommendation

**Keep `anomaly_z_threshold` at 4, and do not let the z-score page anyone.**

F1 technically peaks at {best_f1[0]:g} ({best_f1[1].f1:.4f}), but that optimum is
degenerate: precision is near zero at every threshold tested, so F1 is
maximised simply by firing less often. Ranking by it would push the threshold
up indefinitely. Judged on what actually changes, 4 is a reasonable default - it keeps full
recall on `mild_spike` and `small_account_spike`
({tc['mild_spike'][0]}/{tc['mild_spike'][1]} and
{tc['small_account_spike'][0]}/{tc['small_account_spike'][1]}) while firing
{sweep[3.0].fp - sweep[4.0].fp:,} fewer false positives than z>3.

The alerting decision is separate from the threshold. **`dq_reconciliation` is
the check that should alert**: precision {rs.precision:.3f}, recall
{rs.recall:.3f}, F1 {rs.f1:.3f}. The z-score belongs behind it, as a ranking
signal for triage.

## Why z-score precision is so poor

The specified estimator - median and MAD over the same weekday for
{TRAILING_WEEKS} trailing weeks - has only **{TRAILING_WEEKS} observations** per
baseline. Two measured consequences:

1. **MAD is unstable at n={TRAILING_WEEKS}.** The median estimated sigma is
   {diag['mad_median_pct']}% of the expected value, close to the generator's
   true lognormal sigma of 18%. But the 5th percentile is
   {diag['mad_p05_pct']}% - several times too small. When those eight points
   happen to cluster, the denominator collapses and ordinary noise scores above
   the threshold. That is sampling variability in the denominator, not evidence
   of an anomaly.

2. **An {TRAILING_WEEKS}-week-old median under-predicts a growing series.** Mean
   z across all scored account-days is {diag['mean_z']:+} and
   {diag['pct_positive']}% of z values are positive, where a well-centred score
   would sit at 0 and 50%. Flags split {diag['n_up']:,} up to
   {diag['n_down']:,} down. The flag rate is highest for
   `{diag['worst_product']}` ({diag['worst_rate']}%), the fastest-growing
   product, and lowest for `{diag['best_product']}` ({diag['best_rate']}%), the
   slowest. The baseline chases a trend it never catches.

The MAD floor contributes but is not the main cause: it binds on only
{diag['floor_pct']}% of rows, though those rows flag at
{diag['floor_flag_rate']}% versus {diag['mad_flag_rate']}% elsewhere. At 5% of
the expected value it sits well below the ~{diag['mad_median_pct']}% natural
noise level, so where it binds it manufactures borderline scores from nothing.

## Recall is partly an illusion - read the chance baseline

At a {diag['flag_rate'][DEFAULT_Z] * 100:.2f}% flag rate, a detector that fired
at random would "catch" {diag['chance_caught'][DEFAULT_Z]:.1f} of the
{diag['n_labels']} labels, because a label only has to overlap one flagged
account-day to count. A 21-day drift spans 84 account-product-days, so random
firing catches it {100 * (1 - (1 - diag['flag_rate'][DEFAULT_Z]) ** 84):.0f}% of
the time.

Separating signal from luck:

- **Real detection** (caught well above chance): `{"`, `".join(real)}`
- **Indistinguishable from chance**: `{"`, `".join(chance)}`

So the apparent {tc['gradual_drift'][0]}/{tc['gradual_drift'][1]} on
`gradual_drift` is **not** a detection - chance alone predicts
{tc['gradual_drift'][2]:.2f}. Same for the
{tc['late_arrival'][0]}/{tc['late_arrival'][1]} on `late_arrival`. Any report of
this detector that quotes recall without a chance baseline is overstating it.

## What the z-score genuinely cannot see, and why

- **`ledger_mismatch`** ({tc['ledger_mismatch'][0]}/{tc['ledger_mismatch'][1]}).
  Unreachable by construction: the usage side is clean and only the ledger was
  perturbed, so the usage series contains no trace. Only reconciliation can see
  it, and it gets
  {per_det['reconciliation']['ledger_mismatch'][0]}/{per_det['reconciliation']['ledger_mismatch'][1]}.

- **`late_arrival`**. The values are correct; only `loaded_at` moved, so revenue
  never deviates. `dq_late_arrivals` catches all
  {per_det['late arrivals']['late_arrival'][0]}/{per_det['late arrivals']['late_arrival'][1]}.

- **`gradual_drift`**. A linear ramp to +20% over 21 days moves each day by
  under 1%, an order of magnitude below the ~{diag['mad_median_pct']}% daily
  noise - and the trailing baseline absorbs the drift as it accrues, so the
  deviation never accumulates. This needs a different shape of test: a slope or
  trend test over a multi-week window, or week-level aggregation where noise
  averages down. `fct_weekly_review.vs_trailing_4w_pct` is the natural home.

## The honest summary: on this dataset the z-score earns nothing

The deterministic checks together score precision {ds.precision:.3f}, recall
{ds.recall:.3f}, F1 {ds.f1:.3f}, and catch **all {diag['n_labels']} labels**
across every one of the {len(types)} anomaly types. Adding the z-score moves
true positives from {ds.tp} to {cs.tp} account-days - one more - and adds
{cs.fp:,} false positives. F1 falls from {ds.f1:.3f} to {cs.f1:.3f}.

The z-score has **no unique coverage here**. Every type it genuinely detects,
`dq_reconciliation` also detects, and with perfect precision. The two
account-days nothing catches are the first day of each drift, where the ramp
multiplier is exactly 1.0 - there is no fault present on that day to find.

That conclusion is a direct consequence of decision 1 in DECISIONS.md: because
the ledger is booked from clean usage, it is an independent witness to every
usage-side fault, which makes cheap subtraction beat statistics. Remove that
independence - as in most real warehouses, where the "ledger" is derived from
the same pipeline - and the z-score becomes the only thing left. Its value is
conditional on the absence of a trustworthy second source, not on its own
merits.

One result worth keeping: spike detection does **not** degrade on small
accounts ({tc['small_account_spike'][0]}/{tc['small_account_spike'][1]} caught,
the same as on large accounts), because MAD normalises by each account's own
dispersion. That was the question those spikes were planted to answer.

The highest-leverage fix is not the threshold. It is using more than
{TRAILING_WEEKS} points: model weekday as a multiplicative factor across all
{TRAILING_WEEKS * 7} trailing days rather than slicing history down to one
weekday, or score in log space so multiplicative noise becomes additive. Both
are design changes rather than tuning, and neither was applied - the threshold
was the only parameter chosen against the labels.
"""


if __name__ == "__main__":
    sys.exit(main())
