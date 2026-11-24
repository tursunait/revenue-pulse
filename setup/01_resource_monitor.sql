/* ============================================================================
   Revenue Pulse - 01_resource_monitor.sql
   Caps spend on RP_WH at 5 credits per month.

   Run as ACCOUNTADMIN, after setup/00_setup.sql (RP_WH must already exist).

   WARNING: re-running this file RESETS the monitor's used-credit counter back
   to zero, because CREATE RESOURCE MONITOR has no IF NOT EXISTS form and this
   uses CREATE OR REPLACE. To change the quota on a live monitor without losing
   its usage history, use ALTER instead:
     ALTER RESOURCE MONITOR RP_MONITOR SET CREDIT_QUOTA = <n>;
   ============================================================================ */

USE ROLE ACCOUNTADMIN;

/* Triggers escalate: warn, then stop new work, then kill running work.
     75%  NOTIFY            - email to account admins who opted in, no action
     90%  SUSPEND           - blocks new queries, lets running ones finish
     100% SUSPEND_IMMEDIATE - kills running queries too, hard stop at the cap
   NOTIFY only emails account admins who have enabled notifications in
   Snowsight (Profile -> Notifications), so set that up or the warning is
   silent. */

CREATE OR REPLACE RESOURCE MONITOR RP_MONITOR
  WITH CREDIT_QUOTA = 5
  FREQUENCY         = MONTHLY
  START_TIMESTAMP   = IMMEDIATELY
  TRIGGERS
    ON 75  PERCENT DO NOTIFY
    ON 90  PERCENT DO SUSPEND
    ON 100 PERCENT DO SUSPEND_IMMEDIATE;

/* Attach to RP_WH. This is a warehouse-level monitor, not account-level, so it
   governs only RP_WH and nothing else in the account. */
ALTER WAREHOUSE RP_WH SET RESOURCE_MONITOR = RP_MONITOR;

/* ----------------------------------------------------------------------------
   Verification
   Check "used_credits" / "remaining_credits" in the monitor row, and that the
   warehouse row shows RP_MONITOR in its "resource_monitor" column.
   ---------------------------------------------------------------------------- */

SHOW RESOURCE MONITORS LIKE 'RP_MONITOR';
SHOW WAREHOUSES LIKE 'RP_WH';
