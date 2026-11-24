"""Snowflake connection helper.

Every script in this project connects through `connect()`, which reads
credentials from .env and authenticates RP_SVC with its RSA key pair.

`schema` is a required argument: each script names the schema it works in
rather than inheriting RP_SVC's DEFAULT_NAMESPACE. Relying on that default
makes a script's target silently dependent on account state, and a loader
pointed at the wrong schema is an expensive mistake to notice late.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

import snowflake.connector
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Variables that must be present in .env. SNOWFLAKE_SCHEMA is deliberately
#: absent - scripts pass their schema to connect() explicitly.
REQUIRED_ENV = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PRIVATE_KEY_PATH",
    "SNOWFLAKE_ROLE",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
)


class ConfigError(RuntimeError):
    """Raised when .env or the key file is missing or unusable."""


def fail(message: str) -> NoReturn:
    raise ConfigError(message)


def load_env() -> None:
    """Load .env from the repository root and check the required variables."""
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        fail(f"no .env at {env_file}. Copy it from .env.example and fill it in.")
    load_dotenv(env_file)

    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        fail(f".env is missing: {', '.join(missing)}")


def load_private_key() -> bytes:
    """Read the PKCS#8 key and return it as DER, which the connector wants."""
    key_path = Path(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]).expanduser()
    if not key_path.is_file():
        fail(
            f"no private key at {key_path}. Generate one per setup/README.md, "
            "or fix SNOWFLAKE_PRIVATE_KEY_PATH in .env."
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


def connect(schema: str) -> snowflake.connector.SnowflakeConnection:
    """Connect as RP_SVC, with `schema` set explicitly on the session.

    Args:
        schema: the schema this script works in, e.g. "RAW". Required.
    """
    if not schema:
        raise ValueError("schema is required; pass it explicitly, e.g. connect('RAW')")

    load_env()
    try:
        return snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            private_key=load_private_key(),
            role=os.environ["SNOWFLAKE_ROLE"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=os.environ["SNOWFLAKE_DATABASE"],
            schema=schema,
            client_session_keep_alive=False,
            # These scripts run only synchronous queries, so there is nothing to
            # preserve across logout. Set explicitly: leaving it unset raises a
            # FutureWarning, since the connector's default is due to change.
            enable_server_session_keep_alive_auto_detection=False,
        )
    except snowflake.connector.errors.DatabaseError as exc:
        fail(
            f"could not connect as {os.environ['SNOWFLAKE_USER']}: {exc.msg}\n"
            "Common causes: SNOWFLAKE_ACCOUNT is not the "
            "<orgname>-<account_name> identifier, or the public key was never "
            "attached to the user (see setup/README.md step 3)."
        )
