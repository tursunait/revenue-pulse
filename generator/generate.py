"""Build the four synthetic RAW tables for Revenue Pulse.

Deterministic: every random draw comes from one np.random.default_rng(SEED)
with SEED = 42, so repeated runs produce byte-identical values. Only the date
window shifts, because it ends on the run date by default (override with
RP_END_DATE=YYYY-MM-DD) so freshness checks downstream have recent data.

Tables
------
ACCOUNTS         ~200 accounts, with segment, region, contract, lifecycle dates.
USAGE_EVENTS     one row per active account x day x product.
LEDGER_REVENUE   finance's booked revenue per account per day.
ANOMALY_LABELS   ground truth for every anomaly planted below.

How the ledger relates to usage
-------------------------------
LEDGER_REVENUE is booked from the *clean* usage revenue, before any usage
anomaly is injected. Finance books from contracts and invoices, not from the
telemetry pipeline, so the ledger is an independent source of truth: a spike,
a unit error or a duplicated load makes usage exceed the ledger, and a pipeline
gap makes it fall short. Reconciling the two is therefore a real detection
signal. The ledger_mismatch anomalies are the exception - there the usage side
is clean and the ledger itself is perturbed.

One caveat for whoever writes the reconciliation check: booked_revenue is
rounded to cents, so on a clean account-day it differs from the usage revenue
by at most half a cent. On a $1-5 account-day that is up to ~0.43% in relative
terms, while the planted mismatches are 2-10%. A reconciliation test should
therefore require both a relative and a small absolute threshold, or it will
fire on the smallest accounts every day.

Writes parquet to data/raw/. generator/load.py reads those files and uploads
them. Run this first:

    python generator/generate.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "raw"

SEED = 42
N_ACCOUNTS = 200
N_DAYS = 365

PRODUCTS = ("compute", "storage", "ai_services", "serverless")

#: Share of an account's daily credits by product.
PRODUCT_SHARE = {"compute": 0.55, "storage": 0.20, "ai_services": 0.08, "serverless": 0.17}

#: List price per credit. Storage is cheap per credit, AI services expensive.
PRODUCT_PRICE = {"compute": 2.00, "storage": 0.45, "ai_services": 5.50, "serverless": 1.60}

#: How strongly each product follows the weekday cycle. Storage is provisioned,
#: not run, so it barely moves; compute tracks the working week closely.
PRODUCT_SEASONALITY = {"compute": 1.0, "storage": 0.15, "ai_services": 0.8, "serverless": 0.9}

#: Extra annual growth multiplier on top of segment growth. AI services is the
#: fast-growing line.
PRODUCT_GROWTH = {"compute": 0.0, "storage": 0.25, "ai_services": 1.80, "serverless": 0.45}

SEGMENTS = ("Enterprise", "Commercial", "SMB")
SEGMENT_WEIGHTS = (0.15, 0.35, 0.50)

#: Mean daily credits and spread (lognormal sigma) per segment.
SEGMENT_BASE = {"Enterprise": 420.0, "Commercial": 110.0, "SMB": 26.0}
SEGMENT_SPREAD = {"Enterprise": 0.45, "Commercial": 0.55, "SMB": 0.70}

#: Annual growth rate applied daily, compounded.
SEGMENT_GROWTH = {"Enterprise": 0.28, "Commercial": 0.15, "SMB": 0.04}

#: Negotiated discount off list price.
SEGMENT_DISCOUNT = {"Enterprise": 0.82, "Commercial": 0.92, "SMB": 1.00}

REGIONS = ("NA", "EMEA", "APAC", "LATAM")
REGION_WEIGHTS = (0.45, 0.30, 0.18, 0.07)

CONTRACTS = ("annual_commit", "monthly", "on_demand")
#: Contract mix depends on segment: Enterprise commits, SMB pays as it goes.
CONTRACT_WEIGHTS = {
    "Enterprise": (0.80, 0.15, 0.05),
    "Commercial": (0.45, 0.35, 0.20),
    "SMB": (0.10, 0.30, 0.60),
}

#: Monday..Sunday multiplier on daily credits.
WEEKDAY_FACTOR = np.array([0.98, 1.05, 1.08, 1.06, 0.95, 0.55, 0.50])

#: New accounts ramp from RAMP_FLOOR to full volume over this many days.
RAMP_DAYS = 60
RAMP_FLOOR = 0.15

CHURN_RATE = 0.08
NEW_ACCOUNT_RATE = 0.30

#: Day-to-day noise on credits (lognormal sigma).
NOISE_SIGMA = 0.18

#: Normal pipeline latency: usage lands early the next morning.
LOAD_HOUR = 2
LOAD_JITTER_MINUTES = 45

#: How many of each anomaly type to plant (33 total).
#:
#: The first six types are obvious: large, single-day, high-contrast faults.
#: The last three are deliberately subtle, to stop the detector looking better
#: than it is - a 10x spike is easy, a 1.6x spike on a tiny account is not.
ANOMALY_PLAN = {
    "spike": 6,
    "late_arrival": 5,
    "duplicate_load": 4,
    "ledger_mismatch": 4,
    "pipeline_gap": 3,
    "unit_error": 3,
    # -- subtle --
    "mild_spike": 3,
    "small_account_spike": 3,
    "gradual_drift": 2,
}

#: Mild spikes sit just above normal day-to-day noise (sigma 0.18).
MILD_SPIKE_RANGE = (1.5, 2.0)

#: Small-account spikes use the SAME multiplier range as the obvious spikes, so
#: the only variable is account size. That isolates the question the test is
#: asking: does detection depend on how big the account is?
SMALL_ACCOUNT_QUANTILE = 0.20

#: Gradual drift: a linear ramp to +20% over 21 days. No single day moves much,
#: which is exactly what a day-over-day detector is blind to.
DRIFT_PCT = 0.20
DRIFT_DAYS = 21

#: Leave a warm-up and cool-down margin so rolling baselines have history.
ANOMALY_MARGIN_DAYS = 30


def utc_now() -> datetime:
    """Current UTC time, naive. Every timestamp in this dataset is naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def window() -> tuple[date, date]:
    """Return (start, end) of the 365-day window, inclusive.

    Ends YESTERDAY, not today. Today's usage has not finished accruing, and a
    partial final day would make the newest loaded_at land in the future -
    which silently disables source freshness, since a negative age clears any
    threshold.
    """
    override = os.environ.get("RP_END_DATE")
    end = date.fromisoformat(override) if override else utc_now().date() - timedelta(days=1)
    return end - timedelta(days=N_DAYS - 1), end


def build_accounts(rng: np.random.Generator, start: date, end: date) -> pd.DataFrame:
    """~200 accounts with segment, region, contract, and lifecycle dates."""
    segment = rng.choice(SEGMENTS, size=N_ACCOUNTS, p=SEGMENT_WEIGHTS)
    region = rng.choice(REGIONS, size=N_ACCOUNTS, p=REGION_WEIGHTS)

    contract = np.array(
        [rng.choice(CONTRACTS, p=CONTRACT_WEIGHTS[seg]) for seg in segment]
    )

    # Per-account scale: lognormal spread around the segment mean, so a few
    # large accounts dominate revenue the way they do in reality.
    sigma = np.array([SEGMENT_SPREAD[s] for s in segment])
    base = np.array([SEGMENT_BASE[s] for s in segment])
    scale = base * rng.lognormal(mean=-(sigma**2) / 2, sigma=sigma)

    # 30% of accounts start inside the window (they will ramp); the rest are
    # tenured, having started up to two years before it.
    is_new = rng.random(N_ACCOUNTS) < NEW_ACCOUNT_RATE
    start_offsets = np.where(
        is_new,
        rng.integers(1, N_DAYS - ANOMALY_MARGIN_DAYS, size=N_ACCOUNTS),
        -rng.integers(1, 730, size=N_ACCOUNTS),
    )
    start_date = np.array([start + timedelta(days=int(o)) for o in start_offsets])

    # A few accounts churn partway through the window.
    churns = rng.random(N_ACCOUNTS) < CHURN_RATE
    churn_date: list[date | None] = []
    for i in range(N_ACCOUNTS):
        if not churns[i]:
            churn_date.append(None)
            continue
        earliest = max(start_date[i] + timedelta(days=90), start + timedelta(days=45))
        if earliest >= end:
            churn_date.append(None)
            continue
        span = (end - earliest).days
        churn_date.append(earliest + timedelta(days=int(rng.integers(0, span + 1))))

    return pd.DataFrame(
        {
            "account_id": [f"ACC{i + 1:05d}" for i in range(N_ACCOUNTS)],
            "account_name": [f"Account {i + 1:03d} {seg[:4]}" for i, seg in enumerate(segment)],
            "segment": segment,
            "region": region,
            "contract_type": contract,
            "start_date": pd.to_datetime(start_date),
            "churn_date": pd.to_datetime(churn_date),
            # Not derived downstream: churn_date is NULL for active accounts.
            "credit_scale": scale.round(3),
        }
    )


def clamp_to_now(stamps: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Cap load timestamps at the current UTC time."""
    cap = np.datetime64(utc_now(), "ns")
    return pd.DatetimeIndex(np.minimum(stamps.values.astype("datetime64[ns]"), cap))


def build_usage(
    rng: np.random.Generator, accounts: pd.DataFrame, start: date, end: date
) -> pd.DataFrame:
    """One row per active account x day x product, with clean credits."""
    days = pd.date_range(start, end, freq="D")
    n_prod = len(PRODUCTS)

    # Full cross product, then filter to each account's active window.
    acc_idx = np.repeat(np.arange(N_ACCOUNTS), N_DAYS * n_prod)
    day_idx = np.tile(np.repeat(np.arange(N_DAYS), n_prod), N_ACCOUNTS)
    prod_idx = np.tile(np.arange(n_prod), N_ACCOUNTS * N_DAYS)

    day_values = days.values[day_idx]
    acct_start = accounts["start_date"].values[acc_idx]
    acct_churn = accounts["churn_date"].values[acc_idx]

    active = day_values >= acct_start
    # churn_date is exclusive: no usage on or after the day the account churns.
    has_churn = ~pd.isna(acct_churn)
    active &= ~(has_churn & (day_values >= acct_churn))

    acc_idx, day_idx, prod_idx = acc_idx[active], day_idx[active], prod_idx[active]
    day_values = day_values[active]
    n = acc_idx.size

    segment = accounts["segment"].values[acc_idx]
    product = np.array(PRODUCTS)[prod_idx]

    scale = accounts["credit_scale"].values[acc_idx].astype(float)
    share = np.array([PRODUCT_SHARE[p] for p in PRODUCTS])[prod_idx]

    # Weekday seasonality, damped per product.
    weekday = pd.DatetimeIndex(day_values).day_of_week.values
    season_strength = np.array([PRODUCT_SEASONALITY[p] for p in PRODUCTS])[prod_idx]
    season = 1.0 + (WEEKDAY_FACTOR[weekday] - 1.0) * season_strength

    # Segment growth plus per-product growth, compounded daily across the window.
    t = day_idx / max(N_DAYS - 1, 1)
    seg_growth = np.array([SEGMENT_GROWTH[s] for s in segment])
    prod_growth = np.array([PRODUCT_GROWTH[p] for p in PRODUCTS])[prod_idx]
    growth = (1.0 + seg_growth) ** t * (1.0 + prod_growth * t)

    # New accounts ramp up over their first RAMP_DAYS.
    days_since_start = (
        day_values - accounts["start_date"].values[acc_idx]
    ) / np.timedelta64(1, "D")
    ramp = np.clip(RAMP_FLOOR + (1 - RAMP_FLOOR) * days_since_start / RAMP_DAYS, None, 1.0)

    noise = rng.lognormal(mean=-(NOISE_SIGMA**2) / 2, sigma=NOISE_SIGMA, size=n)

    credits = scale * share * season * growth * ramp * noise
    credits = np.maximum(credits, 0.001).round(3)

    discount = np.array([SEGMENT_DISCOUNT[s] for s in segment])
    price = (np.array([PRODUCT_PRICE[p] for p in PRODUCTS])[prod_idx] * discount).round(4)

    # Normal latency: lands early the next morning, with jitter.
    jitter = rng.integers(-LOAD_JITTER_MINUTES, LOAD_JITTER_MINUTES + 1, size=n)
    loaded_at = (
        pd.DatetimeIndex(day_values)
        + pd.Timedelta(days=1)
        + pd.Timedelta(hours=LOAD_HOUR)
        + pd.to_timedelta(jitter, unit="m")
    )
    # Nothing can have been loaded in the future. Without this clamp the newest
    # loaded_at sits ahead of now, source freshness computes a negative age,
    # and the check passes no matter how stale the pipeline actually is.
    loaded_at = clamp_to_now(loaded_at)

    usage = pd.DataFrame(
        {
            "account_id": accounts["account_id"].values[acc_idx],
            "usage_date": day_values,
            "product": product,
            "credits": credits,
            "price_per_credit": price,
            "loaded_at": loaded_at,
        }
    )
    return usage.sort_values(["usage_date", "account_id", "product"]).reset_index(drop=True)


def build_ledger(usage: pd.DataFrame) -> pd.DataFrame:
    """Finance's booked revenue per account per day, from clean usage."""
    revenue = (usage["credits"] * usage["price_per_credit"]).rename("booked_revenue")
    ledger = (
        usage[["account_id", "usage_date"]]
        .assign(booked_revenue=revenue)
        .groupby(["account_id", "usage_date"], as_index=False)["booked_revenue"]
        .sum()
        .rename(columns={"usage_date": "revenue_date"})
    )
    ledger["booked_revenue"] = ledger["booked_revenue"].round(2)
    ledger["currency"] = "USD"
    ledger["source_system"] = "netsuite"
    # Finance closes the books on a slower cadence than the usage pipeline.
    ledger["loaded_at"] = ledger["revenue_date"] + pd.Timedelta(days=2) + pd.Timedelta(hours=6)
    return ledger.sort_values(["revenue_date", "account_id"]).reset_index(drop=True)


def _small_accounts(usage: pd.DataFrame, quantile: float) -> set[str]:
    """Account ids in the bottom `quantile` by total revenue over the window."""
    revenue = usage["credits"] * usage["price_per_credit"]
    totals = usage.assign(revenue=revenue).groupby("account_id")["revenue"].sum()
    cutoff = totals.quantile(quantile)
    return set(totals[totals <= cutoff].index)


def _candidate_pool(
    rng: np.random.Generator,
    usage: pd.DataFrame,
    start: date,
    end: date,
    *,
    lo_days: int = ANOMALY_MARGIN_DAYS,
    hi_days: int = 5,
    accounts: set[str] | None = None,
    drop_bottom_quantile: float = 0.25,
) -> list[tuple[str, pd.Timestamp]]:
    """(account_id, usage_date) pairs eligible to carry an anomaly.

    Bounded away from both ends of the window so rolling baselines have history
    either side. `hi_days` has to leave room for whatever the anomaly does
    *after* its start date - a 21-day drift or a 7-day load delay must still
    land inside the window, or the label would describe days that hold no data.

    By default the bottom quartile of account-days is dropped as too small to
    anchor an anomaly. The small-account spikes deliberately pass
    drop_bottom_quantile=0 and an `accounts` filter to do the opposite.
    """
    lo = pd.Timestamp(start + timedelta(days=lo_days))
    hi = pd.Timestamp(end - timedelta(days=hi_days))
    interior = usage[(usage["usage_date"] >= lo) & (usage["usage_date"] <= hi)]
    if accounts is not None:
        interior = interior[interior["account_id"].isin(accounts)]

    daily = interior.groupby(["account_id", "usage_date"], as_index=False)["credits"].sum()
    if drop_bottom_quantile > 0:
        daily = daily[daily["credits"] >= daily["credits"].quantile(drop_bottom_quantile)]

    pairs = list(daily[["account_id", "usage_date"]].itertuples(index=False, name=None))
    rng.shuffle(pairs)
    return pairs


def inject_anomalies(
    rng: np.random.Generator,
    usage: pd.DataFrame,
    ledger: pd.DataFrame,
    start: date,
    end: date,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Plant the anomalies in ANOMALY_PLAN and log each one as ground truth.

    Returns (usage, ledger, labels). Each anomaly claims its (account, date)
    pair exclusively, so two anomalies never stack on the same account-day and
    every label describes exactly one detectable event.
    """
    # Several pools, because different anomalies need different room at the
    # end of the window. They share one `claimed` set, so no account-day ever
    # carries two anomalies and every label describes one isolated event.
    pools = {
        "main": _candidate_pool(rng, usage, start, end),
        # +7 days of load delay must still be in the past.
        "late": _candidate_pool(rng, usage, start, end, hi_days=10),
        # a 21-day ramp must fit inside the window.
        "drift": _candidate_pool(rng, usage, start, end, hi_days=DRIFT_DAYS + 5),
        # the opposite selection: only the smallest accounts, keeping their
        # quiet days rather than discarding them.
        "small": _candidate_pool(
            rng, usage, start, end,
            accounts=_small_accounts(usage, SMALL_ACCOUNT_QUANTILE),
            drop_bottom_quantile=0.0,
        ),
    }
    cursors: dict[str, int] = {name: 0 for name in pools}
    claimed: set[tuple[str, pd.Timestamp]] = set()
    labels: list[dict] = []
    drop_mask = np.zeros(len(usage), dtype=bool)
    dup_frames: list[pd.DataFrame] = []

    def claim(span_days: int = 1, pool: str = "main") -> tuple[str, pd.Timestamp]:
        """Take the next unclaimed account-day, reserving `span_days` of it."""
        candidates = pools[pool]
        while cursors[pool] < len(candidates):
            account_id, day = candidates[cursors[pool]]
            cursors[pool] += 1
            span = [(account_id, day + pd.Timedelta(days=i)) for i in range(span_days)]
            if any(key in claimed for key in span):
                continue
            claimed.update(span)
            return account_id, day
        raise RuntimeError(f"ran out of candidate account-days in pool {pool!r}")

    def add_label(**kwargs) -> None:
        labels.append({"label_id": f"ANOM{len(labels) + 1:03d}", **kwargs})

    # -- spike: one product's credits jump 8-15x for a single day ------------
    for _ in range(ANOMALY_PLAN["spike"]):
        account_id, day = claim()
        product = str(rng.choice(PRODUCTS))
        multiplier = float(rng.uniform(8.0, 15.0))
        rows = (
            (usage["account_id"] == account_id)
            & (usage["usage_date"] == day)
            & (usage["product"] == product)
        )
        usage.loc[rows, "credits"] = (usage.loc[rows, "credits"] * multiplier).round(3)
        add_label(
            anomaly_type="spike",
            account_id=account_id,
            product=product,
            start_date=day,
            end_date=day,
            magnitude=round(multiplier, 2),
            direction="up",
            rows_affected=int(rows.sum()),
            description=f"{product} credits multiplied by {multiplier:.1f}x for one day",
        )

    # -- pipeline_gap: 1-2 consecutive days of usage never landed ------------
    for _ in range(ANOMALY_PLAN["pipeline_gap"]):
        span_days = int(rng.integers(1, 3))
        account_id, day = claim(span_days=span_days)
        last_day = day + pd.Timedelta(days=span_days - 1)
        rows = (
            (usage["account_id"] == account_id)
            & (usage["usage_date"] >= day)
            & (usage["usage_date"] <= last_day)
        )
        drop_mask |= rows.values
        add_label(
            anomaly_type="pipeline_gap",
            account_id=account_id,
            product=None,
            start_date=day,
            end_date=last_day,
            magnitude=float(span_days),
            direction="down",
            rows_affected=int(rows.sum()),
            description=f"usage rows missing entirely for {span_days} consecutive day(s)",
        )

    # -- duplicate_load: the day's rows were loaded twice -------------------
    for _ in range(ANOMALY_PLAN["duplicate_load"]):
        account_id, day = claim()
        rows = (usage["account_id"] == account_id) & (usage["usage_date"] == day)
        dup = usage.loc[rows].copy()
        # A loader re-run writes fresh surrogate keys, so the duplicate is only
        # visible as two rows for one account-day-product, not a repeated id.
        dup["loaded_at"] = dup["loaded_at"] + pd.Timedelta(hours=9)
        dup_frames.append(dup)
        add_label(
            anomaly_type="duplicate_load",
            account_id=account_id,
            product=None,
            start_date=day,
            end_date=day,
            magnitude=2.0,
            direction="up",
            rows_affected=int(len(dup)),
            description="every row for the account-day loaded a second time, doubling revenue",
        )

    # -- unit_error: credits recorded in the wrong unit (x1000) -------------
    for _ in range(ANOMALY_PLAN["unit_error"]):
        account_id, day = claim()
        product = str(rng.choice(PRODUCTS))
        rows = (
            (usage["account_id"] == account_id)
            & (usage["usage_date"] == day)
            & (usage["product"] == product)
        )
        usage.loc[rows, "credits"] = (usage.loc[rows, "credits"] * 1000.0).round(3)
        add_label(
            anomaly_type="unit_error",
            account_id=account_id,
            product=product,
            start_date=day,
            end_date=day,
            magnitude=1000.0,
            direction="up",
            rows_affected=int(rows.sum()),
            description=f"{product} credits off by a factor of 1000 (unit conversion error)",
        )

    # -- late_arrival: correct values, but loaded 3+ days late --------------
    for _ in range(ANOMALY_PLAN["late_arrival"]):
        account_id, day = claim(pool="late")
        delay_days = int(rng.integers(3, 8))
        rows = (usage["account_id"] == account_id) & (usage["usage_date"] == day)
        usage.loc[rows, "loaded_at"] = clamp_to_now(
            pd.DatetimeIndex(usage.loc[rows, "loaded_at"] + pd.Timedelta(days=delay_days))
        )
        add_label(
            anomaly_type="late_arrival",
            account_id=account_id,
            product=None,
            start_date=day,
            end_date=day,
            magnitude=float(delay_days),
            direction="none",
            rows_affected=int(rows.sum()),
            description=(
                f"values correct but loaded_at is delayed {delay_days} days beyond "
                f"the normal next-morning latency (total lag ~{delay_days + 1} days)"
            ),
        )

    # -- ledger_mismatch: finance booked 2-10% off the usage revenue -------
    for _ in range(ANOMALY_PLAN["ledger_mismatch"]):
        account_id, day = claim()
        pct = float(rng.uniform(0.02, 0.10))
        sign = 1.0 if rng.random() < 0.5 else -1.0
        rows = (ledger["account_id"] == account_id) & (ledger["revenue_date"] == day)
        if not rows.any():
            continue
        ledger.loc[rows, "booked_revenue"] = (
            ledger.loc[rows, "booked_revenue"] * (1.0 + sign * pct)
        ).round(2)
        add_label(
            anomaly_type="ledger_mismatch",
            account_id=account_id,
            product=None,
            start_date=day,
            end_date=day,
            magnitude=round(sign * pct * 100, 2),
            direction="up" if sign > 0 else "down",
            rows_affected=int(rows.sum()),
            description=(
                f"ledger booked {abs(pct) * 100:.1f}% "
                f"{'above' if sign > 0 else 'below'} the usage revenue"
            ),
        )

    # -- mild_spike: 1.5-2x, just above normal day-to-day noise -------------
    for _ in range(ANOMALY_PLAN["mild_spike"]):
        account_id, day = claim()
        product = str(rng.choice(PRODUCTS))
        multiplier = float(rng.uniform(*MILD_SPIKE_RANGE))
        rows = (
            (usage["account_id"] == account_id)
            & (usage["usage_date"] == day)
            & (usage["product"] == product)
        )
        usage.loc[rows, "credits"] = (usage.loc[rows, "credits"] * multiplier).round(3)
        add_label(
            anomaly_type="mild_spike",
            account_id=account_id,
            product=product,
            start_date=day,
            end_date=day,
            magnitude=round(multiplier, 2),
            direction="up",
            rows_affected=int(rows.sum()),
            description=(
                f"{product} credits multiplied by {multiplier:.2f}x - close to "
                "ordinary daily variation"
            ),
        )

    # -- small_account_spike: a clear spike, but on a bottom-20% account -----
    for _ in range(ANOMALY_PLAN["small_account_spike"]):
        account_id, day = claim(pool="small")
        product = str(rng.choice(PRODUCTS))
        multiplier = float(rng.uniform(8.0, 15.0))
        rows = (
            (usage["account_id"] == account_id)
            & (usage["usage_date"] == day)
            & (usage["product"] == product)
        )
        usage.loc[rows, "credits"] = (usage.loc[rows, "credits"] * multiplier).round(3)
        add_label(
            anomaly_type="small_account_spike",
            account_id=account_id,
            product=product,
            start_date=day,
            end_date=day,
            magnitude=round(multiplier, 2),
            direction="up",
            rows_affected=int(rows.sum()),
            description=(
                f"{product} credits multiplied by {multiplier:.1f}x on a "
                "bottom-20% account by revenue"
            ),
        )

    # -- gradual_drift: linear ramp to +20% over 21 days --------------------
    for _ in range(ANOMALY_PLAN["gradual_drift"]):
        account_id, day = claim(span_days=DRIFT_DAYS, pool="drift")
        last_day = day + pd.Timedelta(days=DRIFT_DAYS - 1)
        rows = (
            (usage["account_id"] == account_id)
            & (usage["usage_date"] >= day)
            & (usage["usage_date"] <= last_day)
        )
        # Ramp factor depends on how far into the drift each day sits, so no
        # single day jumps: the first day is +0%, the last +20%.
        offsets = (
            usage.loc[rows, "usage_date"] - day
        ).dt.days.to_numpy(dtype=float)
        factor = 1.0 + DRIFT_PCT * offsets / max(DRIFT_DAYS - 1, 1)
        usage.loc[rows, "credits"] = (
            usage.loc[rows, "credits"].to_numpy() * factor
        ).round(3)
        add_label(
            anomaly_type="gradual_drift",
            account_id=account_id,
            product=None,
            start_date=day,
            end_date=last_day,
            magnitude=round(DRIFT_PCT * 100, 2),
            direction="up",
            rows_affected=int(rows.sum()),
            description=(
                f"all products ramp linearly to +{DRIFT_PCT * 100:.0f}% over "
                f"{DRIFT_DAYS} days; no single day moves much"
            ),
        )

    # Apply the structural edits last: drop the gap rows, append the duplicates.
    usage = usage.loc[~drop_mask]
    if dup_frames:
        usage = pd.concat([usage, *dup_frames], ignore_index=True)
    usage = usage.sort_values(["usage_date", "account_id", "product"]).reset_index(drop=True)

    # event_id is assigned after the edits so duplicated rows get fresh keys.
    usage.insert(0, "event_id", [f"EVT{i + 1:09d}" for i in range(len(usage))])

    ledger.insert(0, "ledger_id", [f"LED{i + 1:09d}" for i in range(len(ledger))])

    labels_df = pd.DataFrame(labels)
    labels_df["created_at"] = datetime.combine(date.today(), time(0, 0))
    return usage, ledger, labels_df


def build_all(seed: int = SEED) -> dict[str, pd.DataFrame]:
    """Build all four tables. One rng, so the whole dataset is reproducible."""
    rng = np.random.default_rng(seed)
    start, end = window()

    accounts = build_accounts(rng, start, end)
    usage = build_usage(rng, accounts, start, end)
    ledger = build_ledger(usage)
    usage, ledger, labels = inject_anomalies(rng, usage, ledger, start, end)

    return {
        "ACCOUNTS": accounts,
        "USAGE_EVENTS": usage,
        "LEDGER_REVENUE": ledger,
        "ANOMALY_LABELS": labels,
    }


def print_summary(tables: dict[str, pd.DataFrame]) -> None:
    start, end = window()
    print(f"Window: {start} .. {end}  ({N_DAYS} days)   seed={SEED}")
    print()
    print("Row counts")
    for name, df in tables.items():
        print(f"  {name:16} {len(df):>9,}  x {len(df.columns)} cols")

    accounts = tables["ACCOUNTS"]
    churned = accounts["churn_date"].notna().sum()
    print()
    print(f"Accounts: {len(accounts)} total, {churned} churned")
    print("  by segment: " + ", ".join(
        f"{seg}={n}" for seg, n in accounts["segment"].value_counts().items()
    ))

    labels = tables["ANOMALY_LABELS"]
    print()
    print(f"Anomalies planted: {len(labels)}")
    print("  " + ", ".join(
        f"{t}={n}" for t, n in labels["anomaly_type"].value_counts().items()
    ))
    print()
    cols = ["label_id", "anomaly_type", "account_id", "product",
            "start_date", "end_date", "magnitude", "rows_affected"]
    view = labels[cols].copy()
    view["start_date"] = view["start_date"].dt.date
    view["end_date"] = view["end_date"].dt.date
    view["product"] = view["product"].fillna("-")
    print(view.to_string(index=False))


def main() -> int:
    tables = build_all()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(OUT_DIR / f"{name}.parquet", index=False)

    print_summary(tables)
    print()
    print(f"Wrote {len(tables)} parquet files to {OUT_DIR.relative_to(REPO_ROOT)}/")
    print("Next: python generator/load.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
