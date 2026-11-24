/* ============================================================================
   Revenue Pulse - 00_setup.sql
   One-time account bootstrap: database, schemas, warehouse, resource monitor,
   role, and service user.

   Run as ACCOUNTADMIN (resource monitors and users require it).
   Idempotent: safe to re-run. Uses CREATE ... IF NOT EXISTS, followed by an
   ALTER for the warehouse and user so their settings converge on a re-run.

   BEFORE RUNNING: set my_login below to your own Snowflake login name, so
   RP_ROLE is granted to you and you keep interactive access to these objects.
   Run `SELECT CURRENT_USER();` in a worksheet if you are unsure what it is.

   Run setup/01_resource_monitor.sql after this file to cap spend on RP_WH.
   ============================================================================ */

USE ROLE ACCOUNTADMIN;

SET my_login = '<MY_LOGIN_USER>';   -- <<< FILL THIS IN

/* ----------------------------------------------------------------------------
   1. Role
   RP_ROLE is the single role that owns and operates the project. Granted to
   SYSADMIN so the standard admin hierarchy can manage it, and to the human
   operator so you can build interactively.
   ---------------------------------------------------------------------------- */

CREATE ROLE IF NOT EXISTS RP_ROLE
  COMMENT = 'Revenue Pulse: owns and operates all REVENUE_PULSE objects.';

GRANT ROLE RP_ROLE TO ROLE SYSADMIN;
GRANT ROLE RP_ROLE TO USER IDENTIFIER($my_login);

/* ----------------------------------------------------------------------------
   2. Warehouse
   X-Small, suspends after 60s idle, auto-resumes on demand. Created suspended
   so the bootstrap itself does not burn a credit.
   ---------------------------------------------------------------------------- */

CREATE WAREHOUSE IF NOT EXISTS RP_WH
  WAREHOUSE_SIZE        = 'XSMALL'
  WAREHOUSE_TYPE        = 'STANDARD'
  AUTO_SUSPEND          = 60
  AUTO_RESUME           = TRUE
  INITIALLY_SUSPENDED   = TRUE
  MIN_CLUSTER_COUNT     = 1
  MAX_CLUSTER_COUNT     = 1
  STATEMENT_TIMEOUT_IN_SECONDS = 1800
  COMMENT = 'Revenue Pulse: compute for dbt, loaders, and Streamlit.';

/* Converge settings if the warehouse already existed with other values. */
ALTER WAREHOUSE RP_WH SET
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND   = 60
  AUTO_RESUME    = TRUE;

/* ----------------------------------------------------------------------------
   3. Database and schemas
   Owned by RP_ROLE. PUBLIC is dropped: every object belongs in one of the four
   named schemas.
   ---------------------------------------------------------------------------- */

CREATE DATABASE IF NOT EXISTS REVENUE_PULSE
  COMMENT = 'Revenue Pulse: synthetic consumption-revenue pipeline (seed=42).';

USE DATABASE REVENUE_PULSE;

CREATE SCHEMA IF NOT EXISTS RAW
  COMMENT = 'Landed synthetic source data, including ANOMALY_LABELS (eval only).';
CREATE SCHEMA IF NOT EXISTS STAGING
  COMMENT = 'dbt staging models: typed, renamed, lightly cleaned.';
CREATE SCHEMA IF NOT EXISTS MARTS
  COMMENT = 'dbt marts: revenue facts, dimensions, and anomaly detector output.';
CREATE SCHEMA IF NOT EXISTS DQ
  COMMENT = 'Data quality test results and freshness/volume checks.';

DROP SCHEMA IF EXISTS REVENUE_PULSE.PUBLIC;

/* ----------------------------------------------------------------------------
   4. Grants to RP_ROLE
   Ownership of the database and schemas, plus the privileges dbt, write_pandas,
   and Streamlit in Snowflake need to create and replace objects.
   ---------------------------------------------------------------------------- */

GRANT OWNERSHIP ON DATABASE REVENUE_PULSE TO ROLE RP_ROLE COPY CURRENT GRANTS;
GRANT OWNERSHIP ON ALL SCHEMAS IN DATABASE REVENUE_PULSE
  TO ROLE RP_ROLE COPY CURRENT GRANTS;

GRANT USAGE, OPERATE ON WAREHOUSE RP_WH TO ROLE RP_ROLE;
GRANT MONITOR ON WAREHOUSE RP_WH TO ROLE RP_ROLE;

GRANT USAGE ON DATABASE REVENUE_PULSE TO ROLE RP_ROLE;
GRANT USAGE, CREATE SCHEMA ON DATABASE REVENUE_PULSE TO ROLE RP_ROLE;

/* Per-schema object privileges. dbt needs CREATE TABLE/VIEW; write_pandas needs
   CREATE STAGE and CREATE FILE FORMAT; Streamlit needs CREATE STREAMLIT. */
GRANT ALL PRIVILEGES ON SCHEMA REVENUE_PULSE.RAW     TO ROLE RP_ROLE;
GRANT ALL PRIVILEGES ON SCHEMA REVENUE_PULSE.STAGING TO ROLE RP_ROLE;
GRANT ALL PRIVILEGES ON SCHEMA REVENUE_PULSE.MARTS   TO ROLE RP_ROLE;
GRANT ALL PRIVILEGES ON SCHEMA REVENUE_PULSE.DQ      TO ROLE RP_ROLE;

/* Future grants so objects created by any owner stay readable by RP_ROLE. */
GRANT SELECT ON FUTURE TABLES        IN DATABASE REVENUE_PULSE TO ROLE RP_ROLE;
GRANT SELECT ON FUTURE VIEWS         IN DATABASE REVENUE_PULSE TO ROLE RP_ROLE;
GRANT SELECT ON FUTURE MATERIALIZED VIEWS IN DATABASE REVENUE_PULSE TO ROLE RP_ROLE;
GRANT SELECT ON FUTURE DYNAMIC TABLES IN DATABASE REVENUE_PULSE TO ROLE RP_ROLE;

/* Cortex AI_COMPLETE. */
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE RP_ROLE;

/* ----------------------------------------------------------------------------
   5. Service user
   RP_SVC is TYPE = SERVICE: key-pair authentication only, no password, and
   exempt from the MFA policies that apply to human users. Attach its public
   key with the ALTER USER statement in setup/README.md after generating the
   key pair - this script never contains key material.
   ---------------------------------------------------------------------------- */

CREATE USER IF NOT EXISTS RP_SVC
  TYPE              = SERVICE
  DEFAULT_ROLE      = RP_ROLE
  DEFAULT_WAREHOUSE = RP_WH
  DEFAULT_NAMESPACE = REVENUE_PULSE.STAGING
  COMMENT = 'Revenue Pulse: service user for loaders, dbt, and eval scripts.';

ALTER USER RP_SVC SET
  DEFAULT_ROLE      = RP_ROLE
  DEFAULT_WAREHOUSE = RP_WH
  DEFAULT_NAMESPACE = REVENUE_PULSE.STAGING;

GRANT ROLE RP_ROLE TO USER RP_SVC;

/* ----------------------------------------------------------------------------
   6. Verification
   ---------------------------------------------------------------------------- */

SHOW SCHEMAS IN DATABASE REVENUE_PULSE;
SHOW WAREHOUSES LIKE 'RP_WH';
SHOW GRANTS TO ROLE RP_ROLE;
SHOW GRANTS TO USER RP_SVC;
DESCRIBE USER RP_SVC;
