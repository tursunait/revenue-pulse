"""Verify that RP_SVC can connect to Snowflake with its key pair.

Reads credentials from .env, opens a connection, and prints the session's
role, warehouse, and database. Run this after setup/00_setup.sql,
setup/01_resource_monitor.sql, and attaching the public key to RP_SVC.

    python scripts/check_connection.py

Prints no secrets: not the key, not the passphrase, not the account.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

import snowflake.connector
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

REQUIRED = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PRIVATE_KEY_PATH",
    "SNOWFLAKE_ROLE",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)


def fail(message: str) -> NoReturn:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_private_key() -> bytes:
    """Read the PKCS#8 key and return it as DER, which the connector wants."""
    key_path = Path(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]).expanduser()
    if not key_path.is_file():
        fail(
            f"no private key at {key_path}\n"
            "      Generate one per setup/README.md, or fix "
            "SNOWFLAKE_PRIVATE_KEY_PATH in .env."
        )

    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
    try:
        key = serialization.load_pem_private_key(
            key_path.read_bytes(),
            password=passphrase.encode() if passphrase else None,
        )
    except TypeError:
        fail(
            f"the key at {key_path} is encrypted but "
            "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE is empty in .env."
        )
    except ValueError:
        fail(
            f"could not read the key at {key_path}. Either the "
            "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE in .env is wrong, or the file is "
            "not a PKCS#8 PEM key (it should start with "
            "'-----BEGIN ENCRYPTED PRIVATE KEY-----' or "
            "'-----BEGIN PRIVATE KEY-----')."
        )

    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def main() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        fail(f"no .env at {env_file}. Copy it from .env.example and fill it in.")
    load_dotenv(env_file)

    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        fail(f".env is missing: {', '.join(missing)}")

    private_key = load_private_key()

    try:
        conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            private_key=private_key,
            role=os.environ["SNOWFLAKE_ROLE"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=os.environ["SNOWFLAKE_DATABASE"],
            schema=os.environ["SNOWFLAKE_SCHEMA"],
            client_session_keep_alive=False,
            # This script runs only synchronous queries, so there is nothing to
            # preserve across logout. Set explicitly: leaving it unset raises a
            # FutureWarning, since the connector's default is due to change.
            enable_server_session_keep_alive_auto_detection=False,
        )
    except snowflake.connector.errors.DatabaseError as exc:
        fail(
            f"could not connect as {os.environ['SNOWFLAKE_USER']}: {exc.msg}\n"
            "      Common causes: SNOWFLAKE_ACCOUNT is not the "
            "<orgname>-<account_name> identifier, or the public key was never "
            "attached to the user (see setup/README.md step 3)."
        )

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

    # CURRENT_WAREHOUSE() is NULL when the named warehouse does not exist or
    # the role cannot use it, which is easy to miss in the output above.
    if warehouse is None:
        fail(
            f"connected, but no warehouse is active. Check that "
            f"{os.environ['SNOWFLAKE_WAREHOUSE']} exists and that "
            f"{role} has USAGE on it."
        )
    if database is None:
        fail(
            f"connected, but no database is active. Check that "
            f"{os.environ['SNOWFLAKE_DATABASE']} exists and that "
            f"{role} has USAGE on it."
        )


if __name__ == "__main__":
    main()
