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

---

## 4. Duplicate detection needs two layers, not one

**The call.** `stg_usage_events` dedupes on `event_id`, keeping the earliest
`loaded_at`. That alone would have looked like duplicate handling was done. It
isn't, and `dq_duplicates` reports two separate faults:

| Fault | What happened | Caught by |
| --- | --- | --- |
| `event_id` | the same surrogate key arrived twice — a true replay | staging dedupe silently fixes it |
| `business_grain` | one account-day-product has two rows with *different* `event_id`s | nothing in staging; reaches the marts |

**Why it matters.** A loader re-run that re-keys its output produces rows that
are *distinct events* by primary key and *duplicate facts* by business meaning.
Deduping on `event_id` cannot see them — and a `unique` test on `event_id`
passes, which is the dangerous part: the green test implies a guarantee it does
not provide.

So `dq_duplicates` reads `RAW.USAGE_EVENTS` directly, before staging dedupes.
Pointing it at the staging model would have measured the dedupe rather than the
data.

**Evidence.** All four planted `duplicate_load` anomalies show up as 16
`business_grain` rows (4 account-days x 4 products) and zero `event_id` rows —
confirming the generator re-keys on reload, and that the `unique` test on
`event_id` genuinely cannot catch this class.

---

## 5. DQ tests warn; invariant tests error

**The call.** Tests split by what a failure *means*:

- **`error`** — invariants that are bugs if violated: key uniqueness, not-null,
  accepted values, referential integrity, `assert_no_negative_revenue`,
  `assert_weekly_total_matches_daily`.
- **`warn`** — the three DQ exception tests. This dataset has 25 planted
  anomalies, so these are *supposed* to fire.

**Why.** `dbt build` has to stay a meaningful gate. If known anomalies turned
the whole run red, the only options would be ignoring a permanently-failing
build or deleting the anomalies — and then the DQ layer would be exercising
nothing. Splitting by severity means `ERROR=0` still means "the pipeline is
sound" while `WARN=3` means "the data has problems, as designed."

On production data the three DQ tests would be `severity='error'`.

**Evidence.** `PASS=53 WARN=3 ERROR=0`, and `eval/triage_dq.py` maps all 43
exception rows to planted anomalies with **zero unexplained rows** and 25/25
anomalies caught.

### Source freshness: fixed, and expected to warn on a static snapshot

This was originally broken in a way that looked fine. The generator stamped
`loaded_at` as `usage_date + 1 day` and ran usage up to *today*, so the newest
row was dated three hours in the **future**. The computed age was -3 hours,
which clears any threshold — the check passed, and would have passed against a
completely stalled pipeline.

Fixed in the generator: usage now ends **yesterday** (UTC), and `loaded_at` is
clamped to no later than `now`. Freshness is configured `warn_after: 36h`,
`error_after: 72h`, and passes on a fresh load with an age of a few hours.

**Expect it to warn as the snapshot ages.** This is a static dataset: nothing
reloads it, so 36 hours after a `generator/load.py` run the freshness check
starts warning, and after 72 hours it errors. That is the check working, not a
regression — re-run the generator and loader to reset it. It stops being a
nuisance in Layer 2, when the Airflow daily run keeps `RAW` current and
freshness becomes a genuine signal about the pipeline rather than about how
long ago the snapshot was built.

### Note on reconciliation sign convention

`dq_reconciliation.rel_diff_pct` is `(usage - ledger) / ledger`, while
`ANOMALY_LABELS.magnitude` for a `ledger_mismatch` is the perturbation applied
*to the ledger*. The same event therefore reads `+4.50%` in the label and
`-4.31%` in the DQ model — same fault, different denominator. `eval/` should
match on label identity, never by comparing these two numbers.

---

## Environment notes

RP_MONITOR is attached to RP_WH (checked as ACCOUNTADMIN). RP_ROLE can't see
it, so it shows as null from scripts.
