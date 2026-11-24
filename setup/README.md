# Setup

One-time Snowflake bootstrap for Revenue Pulse. You run this once per account.

## 1. Run the bootstrap scripts

Both run in a Snowsight worksheet with the worksheet role set to `ACCOUNTADMIN`
— required because resource monitors and users cannot be created by `SYSADMIN`.

### `setup/00_setup.sql`

Before running, fill in the placeholder near the top so `RP_ROLE` is granted to
your own login and you keep interactive access:

```sql
SET my_login = '<MY_LOGIN_USER>';   -- <<< FILL THIS IN
```

Run `SELECT CURRENT_USER();` in a worksheet if you're not sure what yours is.
Then run all statements. The script creates:

| Object | What it is |
| --- | --- |
| `REVENUE_PULSE` | database, with schemas `RAW`, `STAGING`, `MARTS`, `DQ` (no `PUBLIC`) |
| `RP_WH` | X-Small warehouse, `AUTO_SUSPEND = 60`, `AUTO_RESUME = TRUE` |
| `RP_ROLE` | the project role; owns the database and schemas. Granted to `SYSADMIN` and to your login |
| `RP_SVC` | `TYPE = SERVICE` user for scripts; key-pair auth only |

This file is idempotent — re-run it freely.

### `setup/01_resource_monitor.sql`

Run this second; it needs `RP_WH` to exist. It creates `RP_MONITOR` — 5 credits
per month, attached to `RP_WH` — with escalating triggers:

| At | Action | Effect |
| --- | --- | --- |
| 75% | `NOTIFY` | emails account admins, no action taken |
| 90% | `SUSPEND` | blocks new queries, lets running ones finish |
| 100% | `SUSPEND_IMMEDIATE` | kills running queries too; hard stop |

**Re-running this file resets the monitor's used-credit counter to zero.**
`CREATE RESOURCE MONITOR` has no `IF NOT EXISTS` form, so the file uses
`CREATE OR REPLACE`, and a replaced monitor starts counting from zero — the 5
credits you had already spent this month stop being tracked. To change the
quota on a live monitor without losing its usage history, use `ALTER` instead:

```sql
ALTER RESOURCE MONITOR RP_MONITOR SET CREDIT_QUOTA = 10;
```

`NOTIFY` only emails account admins who have turned on notifications in
Snowsight (**Profile → Notifications**). Without that, the 75% warning is
silent and the first thing you notice is the 90% suspend.

## 2. Generate an RSA key pair

`RP_SVC` is a service user, so it authenticates with a key pair, never a
password. Generate the pair outside the repository, in `~/.snowflake/` — the path
`.env.example` points at — so key material can never be committed:

```bash
mkdir -p ~/.snowflake
chmod 700 ~/.snowflake

# Private key, PKCS#8, encrypted with a passphrase you will be prompted for.
openssl genrsa 2048 \
  | openssl pkcs8 -topk8 -inform PEM -out ~/.snowflake/rsa_key.p8

# Matching public key.
openssl rsa -in ~/.snowflake/rsa_key.p8 \
  -pubout -out ~/.snowflake/rsa_key.pub

chmod 600 ~/.snowflake/rsa_key.p8
```

The first command prompts for an encryption passphrase; the second prompts for
it again to read the key back. Put that passphrase in `.env` as
`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`.

For an unencrypted key, add `-nocrypt` to the `pkcs8` command and leave
`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` empty. An encrypted key is the better
default: the passphrase lives in `.env`, which is gitignored, so the key file
alone is not enough to authenticate.

Snowflake requires a 2048-bit minimum. These are PEM files: the private key
starts with `BEGIN ENCRYPTED PRIVATE KEY` (or `BEGIN PRIVATE KEY` with
`-nocrypt`) and the public key with `BEGIN PUBLIC KEY`, which is what Snowflake
and `snowflake-connector-python` expect.

## 3. Attach the public key to RP_SVC

`ALTER USER ... SET RSA_PUBLIC_KEY` wants the base64 body only — no
`-----BEGIN PUBLIC KEY-----` / `-----END PUBLIC KEY-----` lines and no newlines.
Print exactly that:

```bash
awk '!/-----/' ~/.snowflake/rsa_key.pub | tr -d '\n'
```

Copy the output into this statement in a Snowsight worksheet as `ACCOUNTADMIN`
(or `SECURITYADMIN`):

```sql
ALTER USER RP_SVC SET RSA_PUBLIC_KEY = 'MIIBIjANBgkq...paste the single line...';
```

Then confirm Snowflake recorded a fingerprint:

```sql
DESCRIBE USER RP_SVC;
-- look for RSA_PUBLIC_KEY_FP = SHA256:...
```

`RSA_PUBLIC_KEY_2` exists as a second slot for rotating keys without downtime:
add the new key there, switch clients over, then clear the old slot with
`ALTER USER RP_SVC UNSET RSA_PUBLIC_KEY`.

## 4. Fill in .env

Copy the template and fill it in. `.env` is gitignored; `.env.example` holds no
real values and is the only one that gets committed.

```bash
cp .env.example .env
```

`SNOWFLAKE_ACCOUNT` is the account identifier in `<orgname>-<account_name>`
form, not the full URL. Find it in Snowsight under the account selector in the
bottom-left → the account's "..." menu → **Copy account identifier**.

## 5. Verify the connection

```bash
python scripts/check_connection.py
```

Expected output:

```
Connected to Snowflake with key-pair auth.
  CURRENT_USER()      = RP_SVC
  CURRENT_ROLE()      = RP_ROLE
  CURRENT_WAREHOUSE() = RP_WH
  CURRENT_DATABASE()  = REVENUE_PULSE
  CURRENT_SCHEMA()    = STAGING
```

The script prints no secrets. On failure it names the likely cause — a missing
`.env` variable, a key file that isn't where `SNOWFLAKE_PRIVATE_KEY_PATH` says,
a wrong or missing passphrase, or a public key that was never attached in
step 3.

Note that this starts `RP_WH`, which bills a 60-second minimum.

## Cost notes

`RP_MONITOR` caps `RP_WH` at 5 credits per month and suspends it at 90%, so the
project cannot run away with spend — but a suspended warehouse means pipeline
runs start failing, not silently costing more. Check usage with:

```sql
SHOW RESOURCE MONITORS LIKE 'RP_MONITOR';  -- used_credits / remaining_credits
```

To lift the cap, `ALTER RESOURCE MONITOR RP_MONITOR SET CREDIT_QUOTA = <n>` —
use `ALTER`, not a re-run of `01_resource_monitor.sql`, which would zero the
counter. `AUTO_SUSPEND = 60` keeps idle burn near zero; Snowflake bills a 60-second
minimum each time the warehouse resumes, so very frequent tiny queries cost more
than batching them.

## Never commit

Key files and `.env` stay out of git. Confirm before your first commit:

```bash
git check-ignore -v .env
git status --short   # no *.p8, *.pem, or .env should appear
```
