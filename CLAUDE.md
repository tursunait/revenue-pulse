# Revenue Pulse
Portfolio project: consumption-revenue pipeline on Snowflake.
Stack: Python 3.11, snowflake-connector-python + write_pandas, dbt-snowflake, Streamlit in Snowflake, Cortex AI_COMPLETE.
Rules:
- Credentials only from .env (python-dotenv). Never print or commit secrets or key files.
- Snowflake SQL dialect. Database REVENUE_PULSE; schemas RAW, STAGING, MARTS, DQ.
- Data is synthetic and seeded (seed=42).
- The detector must never read RAW.ANOMALY_LABELS. Only eval/ may.
- Work in small steps. After each step, run it and show me the output.