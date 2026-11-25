# Revenue Pulse

A consumption-revenue pipeline on Snowflake that finds the data-quality faults
hiding inside its own revenue numbers — and proves how well it finds them.

The point of the project is not the dashboard. It is the evaluation: every
detector is scored against a known answer key, so claims like "this catches
anomalies" come with precision, recall, and a chance baseline attached.

---

## Architecture

```
generator/          synthetic source data (seed=42) ──► RAW
  generate.py       200 accounts × 365 days × 4 products + 33 planted anomalies
  load.py           write_pandas → REVENUE_PULSE.RAW

dbt/                RAW ──► STAGING ──► MARTS ──► DQ
  staging/          typed, deduped, revenue computed        (3 views)
  marts/            fct_daily_revenue, fct_weekly_review    (2 tables)
  dq/               reconciliation, duplicates, late arrivals,
                    z-score detector v1 and v2, flagged anomalies (6 tables)
                    59 data tests + source freshness

eval/               the only code allowed to read RAW.ANOMALY_LABELS
  score_detector.py scores every detector, publishes DQ.DETECTOR_SCORECARD
  compare_models.py model comparison for the AI summary
  explain_mover.py  "was that move real?" — checks a mover against the labels

app/                Streamlit in Snowflake (get_active_session, no credentials)
  Weekly Review     KPIs, 13-week trend, top movers, data-health banner
  Data Health       page-tier alerts, review queue, series inspector, scorecard
  AI Summary        stored brief + live numeric and claim verification

scripts/            setup checks, summary generation, table-formatting guard
setup/              one-time bootstrap: database, schemas, X-Small warehouse,
                    5-credit resource monitor, role, key-pair service user
```

**Scale.** `USAGE_EVENTS` 239,548 rows · `LEDGER_REVENUE` 59,887 ·
`ACCOUNTS` 200 · `ANOMALY_LABELS` 33. A full `dbt build` is 70 nodes in ~10
seconds on an X-Small warehouse.

**Two structural rules the code enforces:**

- **The detector never reads the answer key.** `RAW.ANOMALY_LABELS` is readable
  only from `eval/`. The app shows detector quality by reading
  `DQ.DETECTOR_SCORECARD`, which `eval/` publishes — so the dashboard can
  report precision without ever touching a label.
- **The ledger is an independent witness.** `LEDGER_REVENUE` is booked from
  *clean* usage, before anomalies are injected, exactly as finance books from
  contracts rather than telemetry. That independence is what makes
  reconciliation a real signal rather than a tautology.

---

## The data is synthetic

All of it. There is no customer data in this repository and never was.

`generator/generate.py` builds the dataset from a single seeded RNG
(`seed=42`), so a rebuild is reproducible down to the value. It models weekday
seasonality, per-segment growth, ramp-up for newly onboarded accounts, faster
growth in AI products, lognormal noise, and churn — then plants **33 anomalies
of nine types** and records every one in `RAW.ANOMALY_LABELS`:

| Obvious | Subtle |
| --- | --- |
| spike (8–15×) · unit error (×1000) | mild spike (1.5–2×) |
| pipeline gap (1–2 days missing) | spike on a bottom-20% account |
| duplicate load · late arrival (3–7 days) | gradual drift (+20% over 21 days) |
| ledger mismatch (2–10%) | |

The subtle three exist because a detector that only catches 10× spikes looks
far better than it is.

Two consequences of a static synthetic snapshot, both deliberate and both
documented: source freshness starts warning 36 hours after a load (the check
working, not a regression), and Snowflake Cortex `AI_COMPLETE` is unavailable
on a trial account, so summary generation runs locally against the Anthropic
API with a Cortex code path kept behind a flag.

---

## Detector scorecard

Scored at account-day grain over **48,396 account-days** and the **29 labels**
that fall inside detector v2's support. Published to
`DQ.DETECTOR_SCORECARD`; regenerate with `python eval/score_detector.py`.

### Scenario A — a daily ledger is available

| Detector | Precision | Recall | F1 |
| --- | --- | --- | --- |
| **Deterministic checks combined** | **1.000** | **0.971** | **0.986** |
| reconciliation (usage vs ledger) | 1.000 | 0.900 | 0.947 |
| late arrivals | 1.000 | 0.071 | 0.133 |
| duplicates | 1.000 | 0.057 | 0.108 |
| gap rule (zero after non-zero) | 1.000 | 0.057 | 0.108 |
| z-score v2 (\|z\|>4) | 0.197 | 0.200 | 0.199 |

**The headline result is negative, and it is the most interesting thing here:**
the cheap deterministic checks catch all 33 anomalies across all nine types at
perfect precision, and the statistical detector adds *one* account-day for
9,564 false positives. When an independent ledger exists, subtraction beats
statistics. The two account-days nothing catches are each drift's first day,
where the ramp multiplier is exactly 1.0 — there is no fault present yet.

### Scenario B — no daily ledger (finance closes monthly)

The realistic case, and where the statistical detector earns its place.

| Threshold | Precision | Recall | Labels caught | Items/day |
| --- | --- | --- | --- | --- |
| \|z\|>3 | 0.027 | 0.343 | 24/29 | 2.9 |
| \|z\|>4 | 0.278 | 0.314 | 22/29 | 0.3 |
| \|z\|>5 | **0.786** | 0.314 | 22/29 | 0.1 |

Recall rises from 0.186 to 0.314 when the z-score is added, bringing in spikes,
unit errors and small-account spikes that nothing else can see once the ledger
is gone. `ledger_mismatch` becomes structurally undetectable — the usage side is
clean and only the booked figure moved — which is the strongest argument for
obtaining even a provisional daily feed.

### Detector v1 → v2

v1 was the specified estimator: median and MAD over the same weekday for 8
trailing weeks. It was badly calibrated, and the fix came from diagnosing *why*
rather than tuning the threshold:

| | v1 | v2 |
| --- | --- | --- |
| mean z | +0.659 | **+0.036** |
| sd z | 19.17 | **1.03** |
| % positive | 61.3% | 51.3% |
| flag rate at \|z\|>4 | 4.80% | **0.043%** |

Two causes, two changes. An 8-week-old median under-predicts a growing series,
so v2 scores `ln(value+1)` and shifts the baseline by a trend ratio. MAD over 8
points is unstable (5th percentile three times below its median), so v2
estimates spread from 13 weeks of *daily* residuals — valid only because the
expectation already conditions on weekday — floored by the product × segment
cohort. `sd z = 1.03` means a "4 sigma" event is finally rare.

**A chance baseline was necessary.** At a 4.8% flag rate, v1 "caught" a 21-day
drift 98% of the time by luck. Per-type comparison against chance separated
real detection from coincidence and showed v1's apparent recall on three
anomaly types was indistinguishable from random firing. Any recall number
without that baseline overstates the detector.

Full analysis, including the threshold sweep and the recommended alerting
design: [eval/RESULTS.md](eval/RESULTS.md).

---

## Model comparison — the weekly AI summary

`scripts/generate_summary.py` builds a week's aggregates as JSON, asks a model
for a five-bullet brief using only numbers in that JSON, and stores the brief
with its input. The app re-verifies every figure at read time.

Three runs each on week 2026-10-05, same 2,548-token prompt
(`python eval/compare_models.py 2026-10-05 --runs 3`):

| Model | Verified | Latency | Tokens out | Cost/run | Price (in/out per MTok) |
| --- | --- | --- | --- | --- | --- |
| `claude-opus-5-5` | 3/3 | 6.61s | 675 | $0.0237 | $4 / $20 |
| `claude-sonnet-5-5` | 3/3 | **3.22s** | 441 | $0.0095 | $2 / $10 |
| `claude-haiku-5-5` | 3/3 (9/9 over 9 runs) | 7.44s | 1,664 | **$0.0011** | $0.10 / $0.50 |

All three are equally accurate on this task, so the choice is latency against
cost. **Sonnet 5.5 is the default** — fastest, and 2.3× the cost of Haiku rather
than 22× the cost of Opus. Haiku 5.5 is 22× cheaper than Opus but *slower*,
because at this effort it emits ~3.8× more output tokens.

Two caveats worth stating. Haiku 5.5 is priced by prompt length — above 100,000
input tokens the rate is 5× higher, which `MODEL_CONFIG` handles explicitly.
And three runs is a small sample; the pass rates above are a smoke test, not a
reliability claim.

### Verification, not trust

A prompt instruction not to invent numbers is a request, not a control. Storing
the input JSON turns it into one. Two layers run live on the page:

- **Numeric** — every figure is matched back to a value in the aggregates, with
  tolerance derived from how the number was *written*: `$483.0K` is held to the
  hundred, `$482,973` to the dollar. Sign is checked, and a movement word
  counts as a sign, so "fell $1,638" against a rise fails. Small integers pick
  their field from the surrounding words — "1 account-day" and "1 item in the
  review queue" are different fields — and one with no field that fits reads
  *low confidence* rather than verified.
- **Claims** — direction words against the sign of the change, named accounts
  against the role the text gives them, commercial explanations against
  data-quality rebounds, and superlatives against what the aggregates can
  actually rank.

"Ready to send" requires both. The claim layer exists because a summary once
passed numeric verification 24/24 while saying an account was "worth a churn
check" — see below.

---

## How I used AI to build this

I built this with Claude Code, in small steps, reviewing the output of each one
before moving on. The code it produced was mostly fine. What made the project
worth keeping was the review loop: nearly every entry in
[DECISIONS.md](DECISIONS.md) started as something that *looked* right and was
not.

**The most instructive bug produced a perfectly verified falsehood.** A
generated summary said:

> "SMB account ACC00008 fell $13,991.95 (-95.8%) to $606.41 and is worth a
> churn check."

Every figure was correct and present in the source data. The numeric verifier
passed it 24/24. But ACC00008 runs at roughly $550–620 a week; its prior week
read $14,598 only because a planted unit error multiplied one product by 1000.
The fall was the return to normal. The brief would have sent someone to save a
customer that was never leaving.

That produced the rule I now reach for first: **when a model says something
wrong, look at what it was given before reaching for a sterner instruction.**
Three separate failures turned out to be input problems, not prompt problems —
a mover list that ranked data-quality artefacts above real movement, a missing
annotation, and an absent delta the model was computing for itself. All three
were cheaper to fix in the payload than to police in the prose.

**Other findings that became decisions:**

- The dashboard reported **−84.2% week-over-week** — arithmetically correct,
  comparing one day against seven. Partial periods are now excluded from
  comparison rather than scaled, with an assertion that refuses to compare
  weeks of different lengths.
- A 13-week chart counted a planted unit error as **$134,036 of real revenue**.
  There is now a Reported / Excluding-items-under-review toggle, and
  `Reported − Adjusted` was verified to equal the flagged account-day revenue
  to the cent.
- 8,154 clean account-days disagreed with the ledger by up to 0.43%. The cause
  was half a cent of rounding on small accounts. A reconciliation alert needs a
  relative *and* an absolute gate, or it fires on the smallest accounts daily.
- A label in the answer key was **wrong** — it described excess latency as total
  latency, off by a full day. A bug in an answer key does not announce itself;
  it produces plausible metrics that happen to be invalid.
- I scored a detector against **stale tables** once, because a targeted
  `dbt build --select model+` rebuilt descendants but not ancestors. The
  evaluation script now rebuilds every model it reads and refuses to score if
  the build fails.

**And the AI-written checkers were wrong before the AI-written code was.** The
claim checker produced four false positives against three real catches —
reading `4-week` as the quantity 4, letting a thousands separator break clause
splitting, flagging an account named as a data-quality item as a non-mover, and
rejecting a correct sentence because proximity was the wrong signal for
direction. A checker that fires on correct output gets switched off as fast as
one that catches nothing, which is why the test set includes a clean control.

The honest summary of the collaboration: Claude Code wrote most of the lines
quickly and competently, and was also the source of every mistake above. The
value was in refusing to accept "the tests pass" as evidence, and in writing
down what each failure taught — which is what DECISIONS.md is.

---

## Run it

```bash
# 1. Bootstrap Snowflake (once, as ACCOUNTADMIN) — see setup/README.md
#    setup/00_setup.sql then setup/01_resource_monitor.sql

# 2. Credentials: key-pair auth, nothing committed
cp .env.example .env            # fill in, then:
python scripts/check_connection.py

# 3. Generate and load the synthetic data
python generator/generate.py
python generator/load.py

# 4. Build and test the warehouse
./scripts/dbt.sh build
./scripts/dbt.sh source freshness

# 5. Score the detectors (publishes DQ.DETECTOR_SCORECARD)
python eval/score_detector.py

# 6. Generate a weekly brief
python scripts/generate_summary.py 2026-11-16
python eval/check_summaries.py

# 7. Deploy the app — see app/README.md for the Snowsight steps,
#    the role and warehouse to pick, and the ownership gotcha
```

`python scripts/check_app_tables.py` guards against a regression the test suite
could not see: a table rendering a raw float where a formatted number belongs.

---

Stack: Python 3.11 · Snowflake · dbt · Streamlit in Snowflake · Anthropic API
(Cortex `AI_COMPLETE` behind a flag) · synthetic data, `seed=42`.
