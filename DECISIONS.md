# Decisions

Design decisions in Revenue Pulse that were judgement calls rather than
defaults — what the options were, what I picked, and how I know it was right.

Each one is reproducible: `python generator/generate.py` rebuilds the dataset
from `seed=42`, and the numbers quoted below come from that dataset.

---

## 1. The ledger is booked from clean usage, not from the pipeline

**The call.** `RAW.LEDGER_REVENUE` is generated from usage revenue *before* any
usage anomaly is injected. The alternative was to derive it from the final
usage table, which would have been one line of code less.

**Why.** It mirrors how the two systems actually relate. Finance books revenue
from contracts and invoices; it does not read raw telemetry. So the ledger is
an *independent* record, not a copy of the pipeline — and independence is the
whole reason a reconciliation is worth running. Two sources that share an
upstream can only ever agree.

**What it buys.** A single comparison becomes a signed, interpretable signal:

| Observation | What it means |
| --- | --- |
| usage **above** ledger | duplicate load, volume spike, or unit error |
| usage **below** ledger | pipeline gap — usage never landed |
| usage **equals** ledger, ledger looks wrong | the ledger itself drifted |

Had the ledger been derived from final usage, a duplicated load would have
doubled *both* sides and reconciled perfectly — the check would have been blind
to exactly the failure it exists to catch.

**Evidence.** The reconciliation in §2 fires on 22 account-days and every one is
a genuine planted anomaly: 6 spikes, 5 gap-days, 4 duplicate loads, 3 unit
errors, 4 ledger mismatches. The first four categories are *usage* faults that
a self-referential ledger could not have surfaced.

**Cost.** `ledger_mismatch` anomalies have to be planted separately, by
perturbing the ledger on account-days where usage is clean. Worth it.

---

## 2. A reconciliation needs two thresholds, not one

**What I found.** With the ledger in place, almost every clean account-day
still disagreed with usage revenue — 59,720 of 59,860 by some nonzero amount,
8,154 of them by more than 0.01%, the worst by 0.43%. That looked like a bug in
the generator.

It wasn't. The maximum *absolute* disagreement is exactly **$0.005**, half a
cent, on every one of them. `booked_revenue` is rounded to cents, because
finance books in cents. On a $500 account-day that half-cent is 0.001% and
invisible. On a $1.10 account-day it is 0.43%.

**The call.** Keep the rounding — removing it would make the dataset lie about
how finance systems behave — and flag a mismatch only when it clears **both** a
relative and an absolute gate:

```
|booked_revenue - usage_revenue| > 0.5%  AND  > $0.01
```

**Why both.** A percentage-only rule is a noise generator at the small end:
relative error explodes as the denominator shrinks, so the smallest accounts
trip it first and most often. A dollar-only rule is the opposite failure — it
ignores a 6% discrepancy on a large account because the absolute number looks
unremarkable in isolation. Each gate covers the other's blind spot.

**Evidence.** On 59,887 account-days the rule fires 22 times, with **zero false
positives** and 4/4 planted `ledger_mismatch` anomalies caught. The noise floor
sits at 0.4345% against a 0.5% gate, and the smallest planted mismatch is 2.23%
— roughly 5x clear of the noise.

**Honest caveat.** On *this* dataset the dollar gate is redundant: percentage
alone also gives 22 fires and zero false positives. The dollar gate earns its
place as a guard on a specific case — account-days below about **$1.00**, where
half a cent is itself more than 0.5% and the percentage gate stops protecting
anything. There is one such account-day here, and it does not currently trip.
I would rather carry a cheap gate that is dormant than discover the gap in
production.

The 0.5% gate has only **1.15x** margin over the 0.43% noise floor, which is
thinner than I would like. If per-row rounding were ever coarsened, that gate
would need to move before the noise did.

**The underlying point.** An alert that fires on every small account every day
teaches people to ignore the channel. Suppressing known-benign noise is not
cutting corners on rigour — it is what makes the remaining alerts mean
something.

---

## 3. The answer key has to measure the right thing

**The bug.** Each `late_arrival` anomaly delays `loaded_at` by 3–7 days. The
label described it as *"loaded_at is N days after the usage date."*

That was wrong. Normal pipeline latency in this dataset is already ~1.05 days —
usage lands early the following morning. The injected delay stacks on top, so a
label reading `magnitude = 5` described a row whose true lag was **6.08 days**.

**The fix.** The label now reads *"delayed N days beyond the normal
next-morning latency (total lag ~N+1 days),"* stating the baseline it is
measured against.

**Why it matters more than a wording nit.** `ANOMALY_LABELS` is the ground truth
that `eval/` scores the detector against. A detector measuring *total* lag and
a label recording *excess* lag disagree by a full day — so a correct detector
would have been marked wrong, and the precision and recall numbers reported for
this project would have been quietly, confidently false.

Bugs in an answer key don't announce themselves. They produce plausible metrics
that happen to be invalid, which is worse than a crash.

**How it surfaced.** Not by re-reading the code — by validating generated data
against its own labels and printing the measured lag next to the claimed one.
Normal lag `min=1.05, p99=1.11`; labeled days came back at 4.05 and 6.08. The
gap was obvious once the two numbers sat side by side.

**The practice it reflects.** The detector must never read
`RAW.ANOMALY_LABELS` — only `eval/` may. That separation is what makes the
evaluation honest, and it's also why the labels carry the whole burden of being
correct. Nothing downstream can catch their mistakes.
