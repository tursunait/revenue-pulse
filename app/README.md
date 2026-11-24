# Revenue Pulse — Streamlit in Snowflake

Three pages: **Weekly Review**, **Data Health**, **AI Summary** (Step 6 stub).

| File | Role |
| --- | --- |
| `streamlit_app.py` | entry point, navigation, all three pages |
| `queries.py` | every SQL statement, each cached with `st.cache_data(ttl=900)` |
| `components.py` | KPI card, status badge, health banner, stylesheet |
| `environment.yml` | package manifest (stage-based deploy only) |

No credentials anywhere: the session comes from `get_active_session()`. Imports
are limited to `streamlit`, `pandas` and `altair`, all in Snowflake's Anaconda
channel.

## Role and warehouse

| Setting | Value | Why |
| --- | --- | --- |
| Sign-in user | **your own login** | `RP_SVC` is `TYPE = SERVICE` and **cannot sign in to Snowsight**. Key-pair service users have no UI access. |
| Role | **`RP_ROLE`** | It owns `REVENUE_PULSE` and every object the app reads. A Streamlit app runs with **owner's rights**, so whichever role creates it is the role the queries execute as. |
| App location | `REVENUE_PULSE.MARTS` | The app reads mostly from `MARTS`. Any of the four schemas works; `MARTS` avoids creating a fifth. |
| Warehouse | **`RP_WH`** | X-Small, `AUTO_SUSPEND = 60`, capped at 5 credits/month by `RP_MONITOR`. |

### Grants needed: none

`setup/00_setup.sql` already grants `ALL PRIVILEGES` on all four schemas to
`RP_ROLE`, which includes `CREATE STREAMLIT` and `CREATE STAGE`. Verified:

```sql
USE ROLE RP_ROLE;
SHOW GRANTS ON SCHEMA REVENUE_PULSE.MARTS;   -- CREATE STREAMLIT present
```

If you later want someone else to *view* the app without owning it, that is the
only extra grant required:

```sql
GRANT USAGE ON STREAMLIT REVENUE_PULSE.MARTS.REVENUE_PULSE_APP TO ROLE <their_role>;
GRANT USAGE ON WAREHOUSE RP_WH TO ROLE <their_role>;
```

They need no privileges on the underlying tables — owner's rights means the
app queries as `RP_ROLE` regardless of who is looking at it.

## Before deploying

The app reads models and the scorecard, so build them first:

```bash
./scripts/dbt.sh build          # staging, marts, dq
python eval/score_detector.py   # writes DQ.DETECTOR_SCORECARD
```

Without the scorecard the Data Health page shows a "Scorecard not built" badge
instead of the detector table; everything else still works.

## Deploy in Snowsight

1. Sign in to Snowsight at `https://app.snowflake.com` with **your own login**.
2. Top-right **role picker → `RP_ROLE`**. (If it is not listed, run
   `GRANT ROLE RP_ROLE TO USER <your_login>;` as `ACCOUNTADMIN`.)
3. Left nav → **Projects → Streamlit** → **+ Streamlit App**.
4. In the dialog:
   - **App title**: `Revenue Pulse`
   - **App location**: database `REVENUE_PULSE`, schema `MARTS`
   - **App warehouse**: `RP_WH`
   - Click **Create**. Snowsight opens an editor with a sample
     `streamlit_app.py`.
5. Select all in the editor and replace it with the contents of
   `app/streamlit_app.py`.
6. Add the two supporting modules. In the editor's left **file browser**, click
   **+ → New file** and create each one *in the same folder* as
   `streamlit_app.py` (flat imports — `import queries`, `from components import ...`):
   - `queries.py` ← paste `app/queries.py`
   - `components.py` ← paste `app/components.py`
7. Click **Packages** (top of the editor) and add **`altair`**. `streamlit` and
   `pandas` are already included. `environment.yml` is **not** read by the
   Snowsight editor — it only applies to the stage-based deploy below.
8. Click **Run**.

First load takes a few seconds: `RP_WH` has been suspended for 60s and must
resume before the first query returns.

## Alternative: deploy from a stage (scriptable, uses environment.yml)

Snowsight cannot `PUT` files, so this path needs a client — the Python
connector can do it, as can SnowSQL or the `snow` CLI.

```sql
USE ROLE RP_ROLE;
USE WAREHOUSE RP_WH;
CREATE STAGE IF NOT EXISTS REVENUE_PULSE.MARTS.APP_STAGE
  DIRECTORY = (ENABLE = TRUE);
```

```sql
-- from a client that supports PUT
PUT file://app/streamlit_app.py  @REVENUE_PULSE.MARTS.APP_STAGE/revenue_pulse/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://app/queries.py        @REVENUE_PULSE.MARTS.APP_STAGE/revenue_pulse/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://app/components.py     @REVENUE_PULSE.MARTS.APP_STAGE/revenue_pulse/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://app/environment.yml   @REVENUE_PULSE.MARTS.APP_STAGE/revenue_pulse/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
```

```sql
CREATE OR REPLACE STREAMLIT REVENUE_PULSE.MARTS.REVENUE_PULSE_APP
  ROOT_LOCATION = '@REVENUE_PULSE.MARTS.APP_STAGE/revenue_pulse'
  MAIN_FILE     = 'streamlit_app.py'
  QUERY_WAREHOUSE = RP_WH
  TITLE         = 'Revenue Pulse';
```

Redeploying is then a re-`PUT` plus a browser refresh.

## What the pages show

**Weekly Review** — a health banner (red if any page-tier check fired for the
selected week, amber if only review-queue items exist, green otherwise) with a
button through to Data Health; four KPI cards; 13 weeks of revenue by product
and by segment; top movers up and down, ranked by **dollar** change rather than
percentage, because a 400% swing on a $3 account is not a mover.

**Data Health** — a week selector, then page-tier alerts and the review queue
as separate tables, because they mean different things: page-tier checks
compare two independent sources and a break is a real discrepancy, while the
z-score compares a series against its own history and only means "unusual".
Picking a flagged item draws that account-product's daily series with the ±2σ
expected band and the flagged day marked. Account-products with under 13 weeks
of history show **"Not enough history"** rather than a misleading score. The
detector scorecard at the bottom has a scenario toggle: **A** (ledger
available) and **B** (no daily ledger — finance closes monthly).

The scorecard is read from `DQ.DETECTOR_SCORECARD`. The app never reads
`RAW.ANOMALY_LABELS`; `eval/score_detector.py` is the only thing that does, and
it publishes that table so detector quality can be displayed without it.

**AI Summary** — Step 6 placeholder. It states plainly that `AI_COMPLETE` is
unavailable on a Snowflake trial account, which is the current blocker.
