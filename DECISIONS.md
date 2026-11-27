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

Fixed in the generator: usage ends a full day before the snapshot instant, and
`loaded_at` is clamped so nothing is recorded as loaded in the future.
Freshness is configured `warn_after: 36h`, `error_after: 72h`.

The first version of this fix ended the window on **yesterday**, which solved
the future-timestamp problem and created a worse one — see part 12. The window
is now pinned, so freshness measures the age of a fixed snapshot.

**Expect it to warn, and read the warning correctly.** The snapshot is dated
2026-11-23, so the freshness check reports exactly what it should: this data is
old. That is the check working. It becomes a signal about the *pipeline* rather
than about the snapshot only in Layer 2, when an Airflow daily load keeps `RAW`
genuinely current.

### Note on reconciliation sign convention

`dq_reconciliation.rel_diff_pct` is `(usage - ledger) / ledger`, while
`ANOMALY_LABELS.magnitude` for a `ledger_mismatch` is the perturbation applied
*to the ledger*. The same event therefore reads `+4.50%` in the label and
`-4.31%` in the DQ model — same fault, different denominator. `eval/` should
match on label identity, never by comparing these two numbers.

---

## 6. The evaluation script rebuilds everything it reads

**What happened.** After regenerating the dataset with eight new anomalies, I
refreshed the detector with `dbt build --select dq_anomaly_scores+` and scored
it. The numbers were wrong — scored against the *previous* dataset.

`dq_anomaly_scores+` selects that model and its descendants. It does not select
its **ancestors**, so `fct_daily_revenue` was never rebuilt — and because the
marts and DQ models are materialised as **tables**, they kept serving the old
data silently. Views would have reflected the new RAW immediately; tables do
not. Nothing failed, nothing warned.

**How it surfaced.** The symptom was absurd rather than subtle: the
late-arrival detector appeared to catch `ledger_mismatch` anomalies. Those
account-days were the late-arrival days from the *earlier* generator run, still
sitting in a stale table. A plausible-looking wrong number would have shipped.

**The fix.** `eval/score_detector.py` now runs a rebuild itself before reading
anything, and will not score if the build fails:

```python
SELECTOR = [
    "+dq_anomaly_scores+",
    "+dq_anomaly_scores_v2+",
    "+dq_reconciliation+",
    "+dq_duplicates+",
    "+dq_late_arrivals+",
]
```

Note the leading `+` on each. `+model+` selects ancestors *and* descendants;
`model+` alone selects only descendants, which is exactly the mistake above.
And all five models are listed deliberately: `dq_reconciliation`,
`dq_duplicates` and `dq_late_arrivals` are neither ancestors nor descendants of
the scores models, so a selector built around `dq_anomaly_scores` alone would
leave three of the four detectors stale — the same bug in a new place.

**The general lesson.** A table-materialised model is a cache, and every cache
can be stale. A partial selector is an invitation to measure yesterday's data
and believe it. Any script whose output is a number someone will act on should
rebuild its own inputs rather than trust the state of the warehouse.

---

## 7. A partial period is not a small period

**The bug.** The app defaulted to the newest week. The data window ends on a
fixed date, so that week held **1 of 7 days** — and the dashboard reported
**-84.2% week over week** with the trend charts falling off a cliff at the
right edge.

Nothing was broken. 58,792 against 371,924 is exactly -84.2%. The arithmetic
was right and the answer was meaningless, because it compared one day with
seven.

**The call.** Partial periods are excluded from comparison rather than scaled
or annotated:

- The week selector **defaults to the newest complete week**, and labels a
  partial one `2026-11-23 — partial (1 of 7 days)`.
- Week-over-week and trailing-4-week figures are built from **complete weeks
  only** — one `HAVING COUNT(DISTINCT USAGE_DATE) = 7` in the SQL is the single
  place this is enforced.
- Trend charts drop partial weeks entirely.
- A partial week still shows its own revenue and account count — those are
  true facts about the days that exist — but its comparisons render `—` with
  the reason stated.
- Top movers are withheld for a partial week.

**Why not annualise or pro-rate?** Scaling a 1-day week by 7 invents six days
of revenue and reports it as fact. Weekday seasonality alone makes that wrong
by up to 2x here: a Monday scaled by 7 overstates a week containing a weekend,
since weekends run at roughly half of weekdays in this data. A withheld number
prompts a question; a scaled number gets pasted into a deck.

**The guard.** Completeness is measured from the facts, never assumed from the
calendar, and there is an explicit assertion:

```python
def assert_comparable(week_a, week_b) -> None:
    ...
    if a != b:
        raise NotComparable(
            f"refusing to compare {week_a} ({a} days) with {week_b} ({b} days)"
        )
```

The SQL already makes this unreachable. It exists anyway because the failure it
prevents is silent — a wrong percentage looks exactly like a right one. A
raised exception is a far better outcome than a plausible -84%.

**Verified.** `default_week()` returns `2026-11-16` with 7 days; the partial
week returns `wow_pct=None` with a stated reason; `assert_comparable` raises on
the partial-vs-complete pair; and `2026-11-23` is absent from the chart data.

---

## 8. Reported and adjusted are different questions

**The problem.** The 13-week charts counted the planted unit errors and spikes
as revenue. Week 2026-10-05 showed **$482,973** when **$348,937** of it was
defensible — a $134,036 overstatement, visible as a spike a reader would
naturally interpret as a good week.

**The call.** Show both, explicitly labelled, and never silently pick one:

| Mode | What it answers |
| --- | --- |
| **Reported** | What landed. Weeks carrying an open page-tier issue are marked on the chart with a tooltip naming the issue, and the KPI row states how much of the week is under review. |
| **Adjusted** | What we would defend in a review. Account-days with an open amount-affecting issue are dropped, and the caption says how many. |

**What counts as amount-affecting.** Reconciliation breaks, unit errors and
duplicate loads. **Late arrivals are deliberately not dropped**: their values
are correct and only `loaded_at` moved, so excluding them would delete good
revenue to fix a freshness problem.

**The bluntness is deliberate, and it is a real cost.** Adjusted drops the
entire account-day, not just the excess. That over-removes in two cases:

- A `ledger_mismatch` has clean usage and only the booked figure moved, so
  dropping the account-day discards correct usage revenue.
- A `pipeline_gap` has usage missing already; dropping it removes nothing and
  does not restore what never arrived, so Adjusted still understates.

A surgical alternative exists — subtract the estimated excess, `usage - ledger`
— and it was rejected. That number is itself derived from the broken side of
the comparison, so a "corrected" figure would carry the error it is correcting
and look authoritative doing it. Dropping the whole day says *we do not have a
number for this account-day*, which is the truth. Adjusted is a floor, not an
estimate, and the caption says so.

**Verified exactly.** Across the 13 complete weeks, Reported minus Adjusted
equals the summed revenue of the flagged account-days to the cent —
**$152,742.27** against **$152,742.27**, matching week by week.

---

## 9. Listing an item and escalating it are different decisions

**The problem.** Every page-tier check firing turned the Weekly Review banner
red. On 2026-11-16 that meant **"Action needed"** for a **$58.56**
reconciliation break against **$371,924** of revenue — **0.0157%** of the week.
Technically a real break. Not something to interrupt anyone for.

A banner that goes red for $58 trains people to ignore red.

**The call.** Materiality gates the *banner*, never the *list*:

| Banner | Condition |
| --- | --- |
| **red** — "Action needed" | revenue under review **> 0.5% of the week** OR **> $1,000** |
| **amber** — "Minor item under review" | something under review below both gates, or late arrivals, or review-queue items |
| **green** — "All clear" | nothing under review, no late arrivals, empty queue |

Data Health lists **every** page-tier item regardless of size. The banner
answers "do I act now?"; the tables answer "what happened?". Suppressing the
item would be hiding a known break; suppressing the *escalation* is triage.

**Two gates, for the same reason as part 2.** A percentage alone screams on a
quiet week — 0.5% of a small week is a trivial sum. A dollar amount alone
sleeps through a big one — $5,000 against $5m is noise, against $20k it is not.
Requiring either to fire catches both shapes of problem. The thresholds are
constants at the top of `app/streamlit_app.py`:

```python
MATERIAL_SHARE_PCT = 0.5        # % of the week's revenue
MATERIAL_ABS_USD = 5_000.0      # dollars
```

### The absolute gate was recalibrated from $1,000 to $5,000

$1,000 was chosen before the rule was applied to the 13-week chart, and that
application exposed it. Reusing the banner rule for the chart markers turned
**5 of the last 13 weeks red** — three of them for roughly $1,100 against a
~$325,000 week. That is **0.33%**, an amount that does not visibly move the
trend line, and it is the "red for $58" failure one order of magnitude up:
markers on 5 of 13 weeks convey as much as markers on none.

$5,000 is calibrated against the scale of a week in this dataset (~$330,000):
about 1.5% of a week, and an amount worth a person's time. Two alternatives
were considered and rejected:

- **Switch to AND** (require both gates). It produces the same two red weeks
  here, but it can hide a genuinely large dollar amount on a large week — a
  $40,000 break on a $5m week is 0.8%... and a $20,000 break would be 0.4% and
  silently grey. The absolute gate exists precisely to catch that.
- **Lower the share gate instead.** That makes the relative test stricter on
  small weeks, which is the opposite of the problem.

The change is conservative in the direction that matters: it only ever moves a
week from red to amber, and it left every previously reviewed banner outcome
intact — 2026-11-16 amber, 2026-09-28 red, 2026-10-05 red, 2026-09-14 green.
Red markers are now exactly 2026-09-28 (3.93%) and 2026-10-05 (27.8%), the two
weeks whose shape is genuinely distorted. The eight immaterial weeks keep a
small grey dot whose tooltip still names the issue, so nothing is hidden —
it just stops shouting.

**What cannot turn it red.** Late arrivals and review-queue items. Neither
distorts the revenue figure, so neither can clear a revenue materiality gate —
but they leave the banner **amber**, not green. A stalled feed needs an owner
even on a week whose dollars happen to tie out, and the statistical queue is
explicitly a human-judgement list (part 5). Deciding these were green would
have been the wrong reading of "immaterial".

**Verified.** 2026-11-16 ($58.56, 0.0157%) → amber. 2026-10-05 ($134,035.95,
27.8%) → red. 2026-09-28 ($14,088.37, 3.93%) → red. 2026-09-14 ($0) → green.

### The rendering bug underneath it

The same line of copy exposed a Streamlit trap worth recording. `st.markdown`,
`st.caption` and `st.info` treat text between two dollar signs as **LaTeX**, so
`"$59 of $371,924"` rendered as green italic maths with the dollar signs eaten.

The fix differs by context, which is the part that is easy to get wrong:

- **Markdown strings** — escape each `$` as `\$` (`md_money()`, `md()`).
- **Raw-HTML components** — emit `&#36;` instead. A backslash escape would
  print the backslash. The KPI cards were *accidentally* fine because each
  held exactly one `$`, and LaTeX needs a pair; a card showing both a value and
  a dollar delta would have broken.
- **`md_escape` is idempotent.** `md()` escapes the whole string and
  `md_money()` escapes its part, so a naive implementation double-escaped into
  `\\$` and rendered a visible backslash. It now only touches a `$` that is
  not already escaped.

Also from the same review: a share is rendered `<0.1%` rather than `0.0%`,
because `0.0%` reads as "nothing" when the real figure is 0.0157% — small is
not absent.

---

## 10. Generation moved out of Snowflake; verification stayed in the app

**The constraint.** The plan was Snowflake-native: `AI_COMPLETE` inside the
warehouse, no data leaving, no extra credentials. It does not run. Every model
returns the same error on this account:

```
399258 (0A000): AI function _COMPLETE_WITH_PROMPT_HISTORY_LLM
is not available for trial accounts.
```

Confirmed against `claude-4-sonnet`, `claude-3-5-sonnet`, `llama3.1-8b`,
`mistral-large2` and `snowflake-arctic` — it is the account tier, not the model.
The legacy `SNOWFLAKE.CORTEX.COMPLETE` is separately blocked ("New accounts are
blocked from using legacy SNOWFLAKE.CORTEX functions"), so there is no path to a
model from inside this warehouse at all.

**The call.** Split generation from verification instead of dropping the
feature:

| Where | What | Why there |
| --- | --- | --- |
| `scripts/generate_summary.py` (local) | build aggregates, call a model, write `DQ.AI_SUMMARY` | the only place a model is reachable today |
| `app/verifier.py` (in the app) | check every figure against the stored JSON | has to run where the summary is read, not where it was written |

`SUMMARY_BACKEND` selects `anthropic` (default), `cortex`, or `template`. The
Cortex path is written and kept behind the same interface, so upgrading the
account is a one-word change rather than a rewrite — the aggregates, the
prompt, the storage schema and the verifier are all shared.

**Model.** `claude-opus-5-5`, at effort `low`. A five-bullet brief from a 1.9KB
JSON is a simple task, and that model defaults to `medium`, so the level is set
explicitly rather than inherited. Server-side fallbacks are enabled so a policy
decline is retried inside the same call instead of failing the run.

**What crosses the boundary.** Aggregates only: week totals, week-over-week and
trailing comparisons, revenue by product and segment, the top five movers each
way, and the open data-quality items. No event rows, no account list beyond the
named movers. That is a smaller surface than the dashboard already shows, and
it is stored verbatim in `INPUT_JSON` so what was sent is auditable after the
fact.

### Verification is the part that makes this usable

A prompt instruction not to invent numbers is not a control; it is a request.
The stored `INPUT_JSON` turns it into one — every figure in the prose is matched
back to a value in the aggregates, and the page says **"Ready to send"** only if
all of them match.

Three details decide whether that check is trustworthy rather than decorative:

- **Tolerance comes from how the number was written.** `$368.1K` is held to the
  hundred; `$371,924.01` is held to the cent. A fixed tolerance would either
  reject correct roundings or wave through a wrong figure.
- **Identifiers are checked as strings, and their digits are not quantities.**
  `ACC00123` was first read as the number `123`, which failed against every
  week. The digits are now skipped and the identifier is matched against the
  JSON's string values — so a fabricated `ACC09999` is still caught.
- **Dates are skipped.** `2026-11-16` is a label, not three numbers.

**Proven, not assumed.** Template-backed summaries for 2026-11-16, 2026-10-05
and 2026-09-14 all verify clean — which proves the plumbing and nothing more,
since a template's numbers are copied from the JSON. So one stored summary was
deliberately corrupted:

| Edit | Verifier |
| --- | --- |
| `$371,924.01` → `$391,924.01` | **flagged** |
| `-1.4%` → `-4.1%` | **flagged** |
| `ACC00123` → `ACC09999` | **flagged** |
| `$368,125.06` → `$368.1K` (correct, rounded) | **passed**, matched to `trailing_4w_avg` |

Catching the three errors matters; passing the fourth matters as much. A
verifier that flags correct roundings gets switched off within a week.

---

## 11. Numeric verification is necessary but not sufficient

**The sentence that passed every check.** From the 2026-10-05 brief:

> "...while SMB account ACC00008 fell $13,991.95 (-95.8%) to $606.41 and is
> worth a churn check."

Every figure is correct and present in the source aggregates. The numeric
verifier passed it 24/24. The claim is still wrong, and wrong in the most
expensive direction — it would have sent someone to save a customer that was
never leaving.

**What the labels say.** Confirmed with `eval/explain_mover.py ACC00008 2026-10-05`:

| Week | Revenue |
| --- | --- |
| 2026-09-07 | $585.67 |
| 2026-09-14 | $558.83 |
| 2026-09-21 | $546.89 |
| 2026-09-28 | **$14,598.36** |
| 2026-10-05 | $606.41 |
| 2026-10-12 | $622.28 |

ACC00008 runs at roughly $550-620 a week. The prior week read $14,598 because
ANOM014 — a planted unit error multiplying serverless credits by 1000 on
2026-09-30 — inflated it. The "95.8% fall" is the return to baseline. The
account is entirely healthy.

**A true number can carry a false claim.** That is the whole lesson. The
verifier checks that figures exist in the source; it cannot check what they are
said to *mean*. Those are different questions and they need different machinery.

### Fixed at the input first

The model was reasoning correctly from a misleading input: an account-day
inflated 1000x, presented as last week's revenue with no annotation.

1. **Movers are computed excluding items under review.** Measured on
   2026-10-05, the reported basis put two data-quality artefacts at the top of
   the list — the unit error itself (+$133,750.26) and the same account's
   rebound (-$13,991.95) — pushing two real movers out of the top five
   entirely. On the adjusted basis both vanish and ACC00098 and ACC00099 take
   their place.
2. **Every mover carries `prior_week_under_review_usd` and
   `prior_week_issue_type`.** The basis change only removes days that were
   *flagged*; an account recovering from an unflagged problem would still
   appear, so the annotation is the second line of defence and what the claim
   check reads.

**Why both, rather than annotation alone?** Annotation leaves a $13,992 phantom
at the top of the list, where it crowds out the real movement a reader came
for. Recomputing alone trusts that every distortion was flagged. The mover list
answers "what changed in the business"; a data-quality artefact is not business
movement, and it already has its own section.

### The app and the brief share one movers basis

For a while they did not, and a screenshot made it obvious: the Weekly Review
page ranked movers on the reported basis while the generated brief used the
adjusted one, so for 2026-10-05 the dashboard led with ACC00072 at +$133,750
and ACC00008 at -$13,992 while the brief named ACC00164 and ACC00155. Same
week, same question, two different answers — and the dashboard's answer was the
wrong one.

`top_movers` in the app now always passes `basis="excluding_under_review"`,
**regardless of the Reported / Excluding toggle**. The toggle governs the
revenue figures, which are a statement about what landed. The movers list
answers a different question, and the right basis for it does not depend on
which figure the reader is looking at.

Because the figures above the list can still be on the reported basis, the gap
is stated rather than left to be noticed. Naming only absences understated it:

| Account | Reported basis | Movers list |
| --- | --- | --- |
| ACC00072 | +$133,750 (largest riser) | **-$286** (a minor faller) |
| ACC00008 | -$13,992 (largest faller) | **not in list** |

The unit-error account does not vanish — it drops from first to negligible,
which is the more interesting fact. So the line names any mover whose figure
shifts by more than the materiality threshold as well as any that disappears,
and links through to Data Health. On a week with nothing under review the line
does not render at all.

### Then the claim checks

`app/claims.py` tests four things numbers cannot:

| Check | Catches |
| --- | --- |
| `direction` | a direction word that contradicts the sign of `change_usd` |
| `role` | an account given a mover role that it does not hold, or a product/segment called the leader when another leads |
| `rebound` | a mover whose prior week was under review, given a commercial reading |
| `unsupported` | a superlative the aggregates cannot rank, or movement called real because nothing was flagged |

Each was demonstrated against the real sentence where one existed and an
injection otherwise. The `rebound` check flags the ACC00008 sentence as an
**error** when it carries commercial language ("churn") and a **warning** when
it simply fails to name the rebound.

### Three of my own checks were wrong first

Worth recording, because an over-eager checker gets switched off as fast as a
useless one:

- `4-week average` — the bare `4` was read as a quantity and failed against
  every week. Numbers used as unit modifiers are now skipped, as dates already
  were.
- `"led by ACC00072 ... so treat the headline growth as unconfirmed"` — the
  clause splitter kept a thousands separator from breaking the sentence, so
  `growth` was attributed to an account it said nothing about. Separators are
  now protected before splitting.
- `ACC00072` and `ACC00144` named as data-quality items were flagged as
  non-movers. An account can legitimately be named without being a mover; only
  a *mover claim* about a non-mover is an error.
- `"ACC00164 (Enterprise, +$1,638.12) was the largest riser ... and ACC00155
  (Enterprise, -$873.05) was the largest faller"` — a correct sentence, flagged
  because splitting on commas cut `(Enterprise, +$1,638.12)` in half and let
  the `riser` belonging to one account attach to the next. Proximity was the
  wrong signal. The check now reads the **signed amount adjacent to each
  account**, which is how these sentences are actually written and cannot be
  stolen by a neighbouring clause; direction words are only a fallback, scoped
  to the account's own segment with `and` as a boundary.

- `"The other four fallers were ACC00128, ACC00086, ACC00150 and ACC00193, all
  Enterprise. The largest riser was ACC00123..."` — a correct sentence, flagged
  because the segment split did not treat a full stop as a boundary, so a
  faller landed in the riser's segment. Sentence punctuation now splits
  segments. An account named bare in a list consequently gets no direction
  check at all: the word governing it sits in an earlier segment, and reaching
  back for it is how most of these false positives happened.

Five false positives against three real catches. That ratio is the argument for
testing a checker against correct output as hard as against wrong output, and
it is why `eval/test_checks.py` exists — 26 cases, roughly half of them
asserting that a *correct* sentence stays clean, runnable with no warehouse and
no API call.

### The numeric verifier had three holes of its own

Found by reading the verification table on screen rather than the pass/fail
verdict, which is the argument for showing the table at all.

**1. The markdown list dash was read as a minus.** `- $134,035.95` parsed as
−134,035.95. List markers are now stripped before extraction.

**2. Matching ignored sign entirely.** The rule was:

```python
if abs(candidate - value) <= tol or abs(abs(candidate) - abs(value)) <= tol:
```

That second clause existed so "lost $2,055.19" could match a `change_usd` of
−2055.19. It also meant a flipped sign passed. Proven by editing a verified
summary: `+34.6%` → `-34.6%` **passed 22/22**, and flipping a movement word
passed too.

Sign is now checked, with a movement word counting as a sign — "fell $1,638.12"
asserts a negative with no "−" written. The subtlety is scope: a movement word
governs only the **first** number after it, so "down 1.4% from $377,378" signs
the percentage and leaves the prior week's revenue unsigned, which is correct —
$377,378 is not a negative quantity. After the fix both edits fail and the
unmodified summary still passes 22/22.

**3. A small integer matched almost anything.** Both `1`s in one bullet matched
`under_review_account_days`, though the second was "1 item is in the review
queue":

| Figure | Before | After |
| --- | --- | --- |
| `$134,035.95` | `under_review_usd` | `under_review_usd` |
| `1` (account-day) | `under_review_account_days` | `under_review_account_days` |
| `$133,703.16` | `under_review_usd` | `page_tier_items[0].impact_usd` |
| `1` (review queue) | `under_review_account_days` | `review_queue_items` |

For values at or below 10 the field is now chosen by the words around the
number, weighting the **unit word immediately after the digit** four times a
merely-nearby word — "1 account-day" and "1 item" are distinguished by what
follows them, not by what else is in the sentence. A flat context window got
this wrong in a short sentence, where the earlier "account-day" was still in
range of the second number. A figure with no field whose name fits reads **low
confidence** rather than matched: present in the aggregates, but unconfirmed.

The third row of that table was not in the original report. `$133,703.16` fell
inside the 0.5% relative tolerance of `$134,035.95`, so "first hit wins" named
the wrong field while still reporting a correct match. Non-small figures now
prefer the **nearest** candidate, with context as the tie-break.

### Formatting and model, after the fact

The prompt now requires whole dollars or one-decimal thousands — `$482,973` or
`$483.0K`, never cents. Cents in a brief invite false precision on figures that
are themselves aggregates, and the verifier's written-precision tolerance holds
a rounded figure to the precision it was written to, so nothing is lost.

The default model moved from `claude-opus-5-5` to **`claude-sonnet-5-5`** on the
measurement rather than on preference: identical verification pass rate on this
task, the lowest latency of the three tested, and 39% of Opus's cost.

### A fourth verifier bug: the sign of the wrong number

Spotted in the on-screen table, not by a failing test: `$358,801` showed an
asserted sign of `+` in "up +34.6% from $358,801", although the rule says a
movement word governs only the first number after it.

It was a real bug. The code skipped governor *consumption* when a number
carried an explicit sign, so "up" was never claimed by `+34.6%` and fell
through to the next unsigned figure. Harmless with "up" — the `+` happened to
match a positive prior-week revenue — but with "down" it asserted a negative on
the prior week's revenue and would have failed a correct sentence. A number now
consumes its governor whether or not it carries an explicit sign.

The lesson is about where the bug was found. Every automated check passed; the
table on screen was what showed it. Rendering the intermediate state — which
field each figure matched, and what sign was inferred — is what made a silent
wrong answer visible.

### Completeness is a correctness property

A brief can be entirely accurate and still mislead by omission. For 2026-10-05,
"reported revenue up 34.6%" is true and useless when $134,036 of it is a unit
error.

The aggregates now carry `adjusted_wow_pct`, `adjusted_direction` and
`under_review_is_material`, and `check_required_content` refuses "Ready to
send" when a material amount is under review and no bullet states which way
revenue went excluding it. It matches on substance rather than phrasing: a
bullet qualifies if it marks the adjusted basis and either states the direction
in words or quotes the figure.

**And adding it exposed a basis mismatch in the summary that prompted it.** An
earlier brief said the adjusted figure was "below the prior week rather than up"
— comparing the *cleaned* current week against the *uncleaned* prior week. The
prior week had $14,088 under review of its own. On a like-for-like basis the two
disagree about the direction:

| Comparison | 2026-10-05 |
| --- | --- |
| adjusted vs **adjusted** prior ($344,713) | **+1.2% — up** |
| adjusted vs **reported** prior ($358,801) | −2.8% — down |

This is part 7's lesson on a different axis: comparing two periods measured on
different bases produces a confident number that points the wrong way. Only the
like-for-like figure is exposed in the payload, because exposing the other
invites the mistake, and the prompt says explicitly not to compare the adjusted
week against the reported prior.

Adding the check did **not** change the model recommendation — with rule 9b in
the prompt, Opus 5.5, Sonnet 5.5 and Haiku 5.5 all satisfied it 3/3. That is
the input-side pattern again: the omission was a missing field and a missing
instruction, not a model that needed replacing.

### Corrections

Things stated earlier in this file or in a generated brief that later turned
out to be wrong, kept rather than quietly edited away.

**The adjusted-vs-reported basis mismatch.** An earlier brief was praised for
completeness because it said the adjusted figure was "below the prior week
rather than above it", where a later one omitted the comparison entirely. The
omission was real. The statement was also wrong: it compared the *cleaned*
current week against the *uncleaned* prior week, which had $14,088 under
review of its own. On a like-for-like basis the week was **+1.2%, up** — the
opposite direction to what the brief asserted.

It was caught by the rule written to fix the omission, not by review. Adding
`adjusted_wow_pct` forced a decision about which prior week to compare
against, and computing it like-for-like produced a number that disagreed in
sign with the text everyone had already accepted. A requirement to state a
figure is also a requirement to define it, and defining it is what exposed the
error.

The payload exposes only the like-for-like comparison. Publishing both would
let a model pick whichever supports the sentence it has already started.

### The same lesson, found again in the model comparison

Haiku 4.5 failed the numeric verifier on 2 of 6 runs, writing `$124,171.60` and
`$139,290.04`. Neither is in the payload — and neither is random:

```
482,972.64 - 358,801.04 = 124,171.60   (reported - prior week)
482,972.64 - 343,682.60 = 139,290.04   (reported - trailing 4-week average)
```

Correct arithmetic on figures it was given, which rule 1 forbids. The fix was
again at the input: supply `wow_change_usd` and `vs_trailing_4w_change_usd` so
there is nothing to compute. Haiku 4.5 went from **4/6 to 6/6**.

Haiku **5.5** — a generation newer, and what the comparison in the README uses —
passed 9/9 with no numeric failures at all. Its only flagged runs turned out to
be the fourth checker bug above, not model errors.

**The pattern worth taking away:** when a model says something wrong, look at
what it was given before reaching for a sterner instruction. Three separate
failures here — a misleading mover, a missing annotation, an absent delta —
were all cheaper to fix in the payload than to police in the prose.

---

## 12. A seed is not reproducibility

**The bug.** `generate.py` carried `seed=42` and a module docstring promising
"repeated runs produce byte-identical values". It did not. The window ended on
*yesterday*:

```python
end = utc_now().date() - timedelta(days=1)     # the bug
```

So every day the 365-day window slid forward, the synthetic series was built
over different dates, and every planted anomaly landed on a different
account-day. The seed fixed the random *draws*; the dates those draws were
applied to moved.

**How it surfaced.** Screenshots taken two days apart disagreed with the
repository. Week 2026-10-05 went from $482,973 with $134,036 under review to
$348,052 and "All clear" — the same week, the same code, the same seed. Every
published figure moved with it: 2026-11-16 revenue $371,924 → $370,896,
scenario B precision at \|z\|>5 from 20/24 to 11/14.

Nothing failed. `dbt build` passed, the checkers passed, the summaries
verified. The data was internally consistent on any given day and inconsistent
with every document describing it.

**The call.** Pin the window:

```python
END_DATE = date(2026, 11, 23)       # not derived from the clock
```

Overridable with `--end-date` or `RP_END_DATE` for Layer 2 work, with the
docstring stating plainly that a different end date is a different dataset.

Two further clock reads had to go with it, and finding them is the point —
pinning the obvious one would have left the output still varying:

| Read from the clock | Effect | Now |
| --- | --- | --- |
| window end | whole dataset shifts daily | pinned `END_DATE` |
| `loaded_at` clamped to `now` | load timestamps differ per run | clamped to the snapshot instant |
| `created_at` on the labels | parquet bytes differ daily | stamped from the snapshot |

**Proven, not asserted.** `python generator/generate.py --verify-determinism`
builds the dataset twice and compares SHA-256 of all four parquet files. The
claim in the docstring is now a command anyone can run:

```
ACCOUNTS         80c0ceb4ca7fc0fd     yes
USAGE_EVENTS     7cc7fa3b752319af     yes
LEDGER_REVENUE   3fd8542a41a17890     yes
ANOMALY_LABELS   e8594664b5d9f053     yes
DETERMINISTIC: both runs produced byte-identical parquet files.
```

After re-pinning, every figure quoted across README.md, DECISIONS.md and
app/README.md was re-checked against the warehouse — 21 of 21 matched, because
the pinned window is the one they were written from.

### Freshness is Layer 2's job, not the generator's

The window originally chased "yesterday" for a defensible reason: so source
freshness would pass instead of warning on stale data. That was solving the
right problem in the wrong layer.

A generator's job is to produce a **fixed, citable dataset**. Keeping data
current is an *ingestion* concern, and in Layer 2 it is an Airflow daily load
appending one real day — which exercises freshness honestly, because a late or
missing run then means a late or missing run.

Conflating the two cost reproducibility to buy a green check mark on a
synthetic snapshot. The check now warns, correctly, that a snapshot dated
2026-11-23 is old. A warning that is true is worth more than a pass that was
arranged.

---

## Environment notes

RP_MONITOR is attached to RP_WH (checked as ACCOUNTADMIN). RP_ROLE can't see
it, so it shows as null from scripts.
