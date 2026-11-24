"""Load the generated parquet files into REVENUE_PULSE.RAW.

Reads data/raw/*.parquet (written by generator/generate.py) and writes each
table with write_pandas, replacing whatever was there before. Run after:

    python generator/generate.py
    python generator/load.py

Each table is overwritten, not appended, so re-running is safe and leaves RAW
exactly matching the current contents of data/raw/.

Note on types: auto_create_table infers TIMESTAMP_NTZ for every date column,
including the ones that are logically dates (USAGE_DATE, REVENUE_DATE,
START_DATE, CHURN_DATE). That is normal for a raw landing zone - the staging
models cast them to DATE.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.snowflake_conn import ConfigError, connect  # noqa: E402
from generator.generate import OUT_DIR  # noqa: E402

SCHEMA = "RAW"

#: Load order is not enforced by constraints, but keeping dimensions first
#: makes a partial failure easier to reason about.
TABLES = ("ACCOUNTS", "USAGE_EVENTS", "LEDGER_REVENUE", "ANOMALY_LABELS")


def read_table(name: str) -> pd.DataFrame:
    path = OUT_DIR / f"{name}.parquet"
    if not path.is_file():
        raise ConfigError(
            f"missing {path}. Run `python generator/generate.py` first."
        )
    df = pd.read_parquet(path)
    # Upper-case so Snowflake gets conventional unquoted-style identifiers;
    # write_pandas quotes names verbatim, so lower-case here would produce
    # columns that need quoting in every downstream query.
    df.columns = [c.upper() for c in df.columns]
    return df


def main() -> int:
    try:
        tables = {name: read_table(name) for name in TABLES}
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        conn = connect(schema=SCHEMA)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    database = conn.database
    print(f"Loading into {database}.{SCHEMA}\n")

    try:
        for name, df in tables.items():
            success, n_chunks, n_rows, _ = write_pandas(
                conn,
                df,
                table_name=name,
                database=database,
                schema=SCHEMA,
                auto_create_table=True,
                overwrite=True,
                # Keeps naive pandas timestamps from being reinterpreted
                # against a session timezone on the way in.
                use_logical_type=True,
            )
            if not success:
                print(f"FAIL: write_pandas reported failure for {name}", file=sys.stderr)
                return 1
            print(f"  {name:16} wrote {n_rows:>9,} rows in {n_chunks} chunk(s)")

        # Read the counts back from Snowflake rather than trusting the writer.
        print("\nRow counts in Snowflake")
        with conn.cursor() as cur:
            for name in TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {database}.{SCHEMA}.{name}")
                count = cur.fetchone()[0]
                local = len(tables[name])
                flag = "OK" if count == local else f"MISMATCH (local {local:,})"
                print(f"  {database}.{SCHEMA}.{name:16} {count:>9,}  {flag}")

        # ANOMALY_LABELS is ground truth. Reading it here is fine - this is the
        # loader. The detector must never read it; only eval/ may.
        print("\nANOMALY_LABELS as stored")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT LABEL_ID, ANOMALY_TYPE, ACCOUNT_ID,
                       COALESCE(PRODUCT, '-') AS PRODUCT,
                       TO_VARCHAR(START_DATE, 'YYYY-MM-DD') AS START_DATE,
                       TO_VARCHAR(END_DATE, 'YYYY-MM-DD') AS END_DATE,
                       MAGNITUDE, ROWS_AFFECTED, DESCRIPTION
                FROM {database}.{SCHEMA}.ANOMALY_LABELS
                ORDER BY ANOMALY_TYPE, LABEL_ID
                """
            )
            rows = cur.fetchall()
            header = f"  {'LABEL':8} {'TYPE':16} {'ACCOUNT':10} {'PRODUCT':12} {'DATES':24} {'MAG':>9} {'ROWS':>5}"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for label, kind, acct, product, d0, d1, mag, n_rows, _desc in rows:
                dates = d0 if d0 == d1 else f"{d0} .. {d1}"
                print(
                    f"  {label:8} {kind:16} {acct:10} {product:12} {dates:24} "
                    f"{float(mag):>9.2f} {n_rows:>5}"
                )
            print(f"\n  {len(rows)} anomalies")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
