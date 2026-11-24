"""Verify that RP_SVC can connect to Snowflake with its key pair.

Opens a connection through the shared helper and prints the session's role,
warehouse, and database. Run this after setup/00_setup.sql,
setup/01_resource_monitor.sql, and attaching the public key to RP_SVC.

    python scripts/check_connection.py

Prints no secrets: not the key, not the passphrase, not the account.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.snowflake_conn import ConfigError, connect  # noqa: E402

SCHEMA = "STAGING"


def main() -> int:
    try:
        conn = connect(schema=SCHEMA)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            user, role, warehouse, database, schema = cur.execute(
                """
                SELECT CURRENT_USER(),
                       CURRENT_ROLE(),
                       CURRENT_WAREHOUSE(),
                       CURRENT_DATABASE(),
                       CURRENT_SCHEMA()
                """
            ).fetchone()
    finally:
        conn.close()

    print("Connected to Snowflake with key-pair auth.")
    print(f"  CURRENT_USER()      = {user}")
    print(f"  CURRENT_ROLE()      = {role}")
    print(f"  CURRENT_WAREHOUSE() = {warehouse}")
    print(f"  CURRENT_DATABASE()  = {database}")
    print(f"  CURRENT_SCHEMA()    = {schema}")

    # CURRENT_WAREHOUSE() and CURRENT_DATABASE() come back NULL when the object
    # does not exist or the role lacks USAGE on it. Snowflake connects anyway,
    # so this is easy to miss in the output above.
    for label, value, name in (
        ("warehouse", warehouse, "SNOWFLAKE_WAREHOUSE"),
        ("database", database, "SNOWFLAKE_DATABASE"),
        ("schema", schema, f"SCHEMA = {SCHEMA!r} in this script"),
    ):
        if value is None:
            print(
                f"FAIL: connected, but no {label} is active. Check that the "
                f"{label} named by {name} exists and that {role} has USAGE on it.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
