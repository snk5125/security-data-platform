# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Layer: Host Authentications (Cross-Platform)
# -----------------------------------------------------------------------------
# Normalizes authentication events from Linux (host_auth) and Windows
# (host_windows_events) bronze tables into a unified silver table.
#
# Linux auth events: SSH login/logout, sudo, su (from syslog auth.log)
# Windows auth events: Logon (4624), Failed Logon (4625), Logoff (4634),
#                      Explicit Credentials (4648)
#
# Source tables:  security_poc.bronze.host_auth
#                 security_poc.bronze.host_windows_events (class_uid=3002)
# Target table:   security_poc.silver.host_authentications
# Schedule:       Every 15 min (Delta readStream with availableNow)
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_silver_host_common

# COMMAND ----------

dbutils.widgets.text("catalog_name", "security_poc", "Catalog Name")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

catalog_name = dbutils.widgets.get("catalog_name")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

target_table = f"{catalog_name}.silver.host_authentications"
checkpoint_dir = f"{checkpoint_base}/silver_host_auth"

print(f"Target: {target_table}")
print(f"Checkpoint: {checkpoint_dir}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, when, current_timestamp

# =============================================================================
# SOURCE 1: Linux Auth Events (bronze.host_auth)
# =============================================================================
# Parse message field for SSH, sudo, su patterns.
# Extract source_ip from "from X.X.X.X" pattern.
# Detect auth_method from message keywords.

def transform_linux_auth(df):
    """Transform bronze.host_auth rows into silver auth schema."""

    # Filter out non-auth noise from journald:
    #  - "-- No entries --" (empty journald output)
    #  - "Session X logged out" / "Removed session X" (no username, low value)
    df = df.filter(
        ~col("message").contains("-- No entries --") &
        ~col("message").rlike(r"Session \d+ logged out") &
        ~col("message").rlike(r"Removed session \d+")
    )

    # Parse the actual OS user from the message text.  The actor.user.name
    # field from journald sources contains the Databricks job runner identity
    # (e.g. sethdemoa@gmail.com) instead of the real OS user, so we parse
    # from sshd/sudo/logind message patterns and fall back to actor.user.name.
    parsed_user = (
        # sshd: "Accepted publickey for ec2-user from ..."
        when(F.regexp_extract(col("message"), r"(?:Accepted|Failed)\s+\S+\s+for\s+(\S+)", 1) != "",
             F.regexp_extract(col("message"), r"(?:Accepted|Failed)\s+\S+\s+for\s+(\S+)", 1))
        # sshd: "Postponed publickey for ec2-user from ..."
        .when(F.regexp_extract(col("message"), r"Postponed\s+\S+\s+for\s+(\S+)", 1) != "",
              F.regexp_extract(col("message"), r"Postponed\s+\S+\s+for\s+(\S+)", 1))
        # systemd-logind: "New session 74 of user ec2-user."
        .when(F.regexp_extract(col("message"), r"of user (\S+)\.", 1) != "",
              F.regexp_extract(col("message"), r"of user (\S+)\.", 1))
        # sudo: "ec2-user : ... COMMAND=..."
        .when(F.regexp_extract(col("message"), r"sudo:\s+(\S+)\s+:", 1) != "",
              F.regexp_extract(col("message"), r"sudo:\s+(\S+)\s+:", 1))
        # PAM/su: "acct=ec2-user"
        .when(F.regexp_extract(col("message"), r"acct=(\S+)", 1) != "",
              F.regexp_extract(col("message"), r"acct=(\S+)", 1))
        # sshd: "Invalid user badguy from ..."
        .when(F.regexp_extract(col("message"), r"Invalid user (\S+)", 1) != "",
              F.regexp_extract(col("message"), r"Invalid user (\S+)", 1))
        .otherwise(col("actor.user.name"))
    )

    return df.select(
        compute_event_id("host_authentications"),
        col("time"),
        col("device.hostname").alias("hostname"),
        lit("Linux").alias("os_type"),
        parsed_user.alias("user"),

        # Extract source IP from patterns like "from 10.0.1.5 port 22"
        F.regexp_extract(col("message"), r"from\s+(\d+\.\d+\.\d+\.\d+)", 1).alias("source_ip"),

        # Detect auth method
        when(col("message").contains("publickey"), lit("ssh_publickey"))
        .when(col("message").contains("password"), lit("ssh_password"))
        .when(F.lower(col("message")).contains("sudo"), lit("sudo"))
        .when(F.lower(col("message")).contains("su["), lit("su"))
        .when(F.lower(col("message")).contains("su:"), lit("su"))
        .otherwise(lit("unknown")).alias("auth_method"),

        # Detect action
        when(F.lower(col("message")).contains("accepted"), lit("login"))
        .when(F.lower(col("message")).contains("session opened"), lit("login"))
        .when(F.lower(col("message")).contains("new session"), lit("login"))
        .when(F.lower(col("message")).contains("session closed"), lit("logout"))
        .when(F.lower(col("message")).contains("logged out"), lit("logout"))
        .when(F.lower(col("message")).contains("removed session"), lit("logout"))
        .when(F.lower(col("message")).contains("sudo"), lit("escalation"))
        .when(F.lower(col("message")).rlike(r"su[\[:]"), lit("escalation"))
        .when(F.lower(col("message")).contains("failed"), lit("failed_login"))
        .when(F.lower(col("message")).contains("invalid user"), lit("failed_login"))
        .otherwise(lit("unknown")).alias("action"),

        # Status from bronze status_id
        when(col("status_id") == 1, lit("success"))
        .when(col("status_id") == 2, lit("failure"))
        .otherwise(lit("unknown")).alias("status"),

        lit(None).cast("int").alias("event_code"),
        lit("host_auth").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# SOURCE 2: Windows Auth Events (bronze.host_windows_events, class_uid=3002)
# =============================================================================
# Filter to auth-related EventCodes via win_event_type label or class_uid.
# Extract source_ip from "Source Network Address:" pattern.

def transform_windows_auth(df):
    """Transform bronze.host_windows_events auth rows into silver auth schema."""
    # Route using win_event_type label with regex fallback
    win_type = get_win_event_type()
    df_auth = df.filter(
        (col("class_uid") == 3002) &
        ((win_type == "auth") | col("class_uid").isNotNull())
    )

    # Extract EventCode from message for classification
    df_auth = df_auth.withColumn("_event_code",
        when(
            F.regexp_extract(col("message"), r"EventCode=(\d+)", 1) != "",
            F.regexp_extract(col("message"), r"EventCode=(\d+)", 1).cast("int")
        )
    )

    # Filter to specific auth EventCodes
    df_auth = df_auth.filter(
        col("_event_code").isin(4624, 4625, 4634, 4647, 4648) |
        col("_event_code").isNull()  # include if EventCode not parseable
    )

    return df_auth.select(
        compute_event_id("host_authentications"),
        col("time"),
        col("device.hostname").alias("hostname"),
        lit("Windows").alias("os_type"),
        col("actor.user.name").alias("user"),

        # Extract source IP from Windows logon event
        F.regexp_extract(col("message"), r"Source Network Address:\s+(\S+)", 1).alias("source_ip"),

        # Auth method from EventCode
        when(col("_event_code") == 4648, lit("runas"))
        .when(col("_event_code").isin(4624, 4625), lit("console"))
        .otherwise(lit("unknown")).alias("auth_method"),

        # Action from EventCode
        when(col("_event_code").isin(4624, 4648), lit("login"))
        .when(col("_event_code") == 4625, lit("failed_login"))
        .when(col("_event_code").isin(4634, 4647), lit("logout"))
        .otherwise(lit("unknown")).alias("action"),

        # Status from bronze
        when(col("status_id") == 1, lit("success"))
        .when(col("status_id") == 2, lit("failure"))
        .otherwise(lit("unknown")).alias("status"),

        col("_event_code").alias("event_code"),
        lit("host_windows_events").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# INGESTION — Delta readStream from both bronze tables
# =============================================================================

# Source 1: Linux auth
linux_source = f"{catalog_name}.bronze.host_auth"
linux_checkpoint = f"{checkpoint_dir}/linux"
print(f"Reading Linux auth from {linux_source}...")

try:
    linux_raw = spark.readStream.table(linux_source)
    linux_silver = transform_linux_auth(linux_raw)
    (
        linux_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", linux_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Linux auth done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Linux auth skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# Source 2: Windows auth
windows_source = f"{catalog_name}.bronze.host_windows_events"
windows_checkpoint = f"{checkpoint_dir}/windows"
print(f"Reading Windows auth from {windows_source}...")

try:
    windows_raw = spark.readStream.table(windows_source)
    windows_silver = transform_windows_auth(windows_raw)
    (
        windows_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", windows_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Windows auth done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Windows auth skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION
# =============================================================================
try_optimize(spark, target_table, ["time", "hostname", "source_ip"])
print(f"Silver host authentications complete. Target: {target_table}")
