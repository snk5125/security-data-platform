# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Layer: Host Account Changes (Cross-Platform)
# -----------------------------------------------------------------------------
# Normalizes user account lifecycle events from Windows (host_windows_events)
# and Linux (host_auth) bronze tables into a unified silver table.
#
# Windows account events: EventCode 4720/4722/4723/4724/4725/4726 (class_uid=3001)
#   — Create, Enable, Password Change, Password Reset, Disable, Delete
# Linux account events: syslog messages from useradd, userdel, passwd, usermod
#   — Extracted from host_auth where the message matches account-management commands
#
# Source tables:  security_poc.bronze.host_windows_events (class_uid=3001)
#                 security_poc.bronze.host_auth            (Linux account commands)
# Target table:   security_poc.silver.host_account_changes
# Schedule:       Every 15 min (Delta readStream with availableNow)
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_silver_host_common

# COMMAND ----------

dbutils.widgets.text("catalog_name", "security_poc", "Catalog Name")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

catalog_name = dbutils.widgets.get("catalog_name")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

target_table = f"{catalog_name}.silver.host_account_changes"
checkpoint_dir = f"{checkpoint_base}/silver_host_accounts"

print(f"Target: {target_table}")
print(f"Checkpoint: {checkpoint_dir}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, when, current_timestamp

# =============================================================================
# SOURCE 1: Windows Account Management Events (bronze.host_windows_events)
# =============================================================================
# Filter on class_uid=3001 (Account Change in OCSF).  Map EventCode to the
# normalized action vocabulary.  Target user is extracted from the Windows
# event message — the "Account Name:" field typically appears twice (acting
# user first, target user second), so we use a capture group that skips the
# first occurrence.

def transform_windows_accounts(df):
    """Transform bronze.host_windows_events account-change rows into silver schema."""
    df_acct = df.filter(col("class_uid") == 3001)

    # Extract EventCode for action mapping
    df_acct = df_acct.withColumn(
        "_event_code",
        when(
            F.regexp_extract(col("message"), r"EventCode=(\d+)", 1) != "",
            F.regexp_extract(col("message"), r"EventCode=(\d+)", 1).cast("int")
        )
    )

    # Map standard account-management EventCodes to canonical action names
    action_col = (
        when(col("_event_code") == 4720, lit("create"))
        .when(col("_event_code") == 4722, lit("enable"))
        .when(col("_event_code") == 4723, lit("password_change"))
        .when(col("_event_code") == 4724, lit("password_reset"))
        .when(col("_event_code") == 4725, lit("disable"))
        .when(col("_event_code") == 4726, lit("delete"))
        .otherwise(lit("unknown"))
    )

    # Target account name — in Windows security events the second "Account Name:"
    # belongs to the target (the first is the acting account's section).
    # Pattern: skip everything up to "Target Account:" then grab Account Name.
    target_user_col = when(
        F.regexp_extract(col("message"), r"Target Account:.*?Account Name:\s+(\S+)", 1) != "",
        F.regexp_extract(col("message"), r"Target Account:.*?Account Name:\s+(\S+)", 1)
    ).otherwise(lit(None))

    # Synthesize a message string for compute_event_id (requires col("message"))
    # Use the raw message column directly — it contains the full event text.

    return df_acct.select(
        compute_event_id("host_account_changes"),
        col("time"),
        col("device.hostname").alias("hostname"),
        lit("Windows").alias("os_type"),
        col("actor.user.name").alias("acting_user"),
        target_user_col.alias("target_user"),
        action_col.alias("action"),
        lit("host_windows_events").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# SOURCE 2: Linux Account Management Commands (bronze.host_auth)
# =============================================================================
# Syslog auth.log captures PAM/useradd/userdel/passwd/usermod invocations.
# Filter on keywords in the message; map command name to canonical action.
# The username argument typically follows the command name.
# acting_user is the session owner from actor.user.name (often root).

def transform_linux_accounts(df):
    """Transform bronze.host_auth Linux account-command rows into silver schema."""
    # Restrict to rows that carry an account-management command
    df_acct = df.filter(
        F.lower(col("message")).rlike(r"useradd|userdel|passwd|usermod|groupadd")
    )

    # Map the command verb to the canonical action vocabulary
    action_col = (
        when(F.lower(col("message")).contains("useradd"), lit("create"))
        .when(F.lower(col("message")).contains("userdel"), lit("delete"))
        .when(F.lower(col("message")).contains("passwd"), lit("password_change"))
        .when(F.lower(col("message")).contains("usermod"), lit("update"))
        .when(F.lower(col("message")).contains("groupadd"), lit("create"))
        .otherwise(lit("unknown"))
    )

    # Extract the target username — typically the last non-whitespace token
    # following the command verb on the line.
    target_user_col = when(
        F.regexp_extract(
            col("message"),
            r"(?:useradd|userdel|passwd|usermod|groupadd)\s+(?:\S+\s+)*?(\S+)$",
            1
        ) != "",
        F.regexp_extract(
            col("message"),
            r"(?:useradd|userdel|passwd|usermod|groupadd)\s+(?:\S+\s+)*?(\S+)$",
            1
        )
    ).otherwise(lit(None))

    return df_acct.select(
        compute_event_id("host_account_changes"),
        col("time"),
        col("device.hostname").alias("hostname"),
        lit("Linux").alias("os_type"),
        # acting_user defaults to "root" when actor.user.name is absent —
        # account management commands typically run as root on Linux
        F.coalesce(col("actor.user.name"), lit("root")).alias("acting_user"),
        target_user_col.alias("target_user"),
        action_col.alias("action"),
        lit("host_auth").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# INGESTION — Delta readStream from both bronze tables
# =============================================================================

# Source 1: Windows account management events
windows_source = f"{catalog_name}.bronze.host_windows_events"
windows_checkpoint = f"{checkpoint_dir}/windows"
print(f"Reading Windows account events from {windows_source}...")

try:
    windows_raw = spark.readStream.table(windows_source)
    windows_silver = transform_windows_accounts(windows_raw)
    (
        windows_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", windows_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Windows account events done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Windows account events skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# Source 2: Linux account management commands
linux_source = f"{catalog_name}.bronze.host_auth"
linux_checkpoint = f"{checkpoint_dir}/linux"
print(f"Reading Linux account events from {linux_source}...")

try:
    linux_raw = spark.readStream.table(linux_source)
    linux_silver = transform_linux_accounts(linux_raw)
    (
        linux_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", linux_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Linux account events done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Linux account events skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION
# =============================================================================
try_optimize(spark, target_table, ["time", "hostname"])
print(f"Silver host account changes complete. Target: {target_table}")
