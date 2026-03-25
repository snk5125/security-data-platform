# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Layer: Host System Events (Cross-Platform)
# -----------------------------------------------------------------------------
# Normalizes general/miscellaneous system events from Linux (host_syslog,
# host_auditd residual) and Windows (host_windows_events system/app channels)
# bronze tables into a unified silver table.
#
# This is the "catch-all" silver layer for host telemetry — auth, process, and
# account events are handled by earlier notebooks; everything else lands here.
#
# Linux syslog:      All rows from bronze.host_syslog (kernel, daemon, cron, etc.)
# Windows system:    host_windows_events with class_uid=6003 (system/app channels)
# Linux auditd misc: host_auditd rows NOT classified as process/auth/account
#                    (PATH, CWD, CONFIG_CHANGE, NETFILTER, etc.)
#
# Source tables:  security_poc.bronze.host_syslog         (all rows)
#                 security_poc.bronze.host_windows_events  (class_uid=6003)
#                 security_poc.bronze.host_auditd          (non-process/auth/account)
# Target table:   security_poc.silver.host_system_events
# Schedule:       Every 15 min (Delta readStream with availableNow)
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_silver_host_common

# COMMAND ----------

dbutils.widgets.text("catalog_name", "security_poc", "Catalog Name")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

catalog_name = dbutils.widgets.get("catalog_name")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

target_table = f"{catalog_name}.silver.host_system_events"
checkpoint_dir = f"{checkpoint_base}/silver_host_system"

print(f"Target: {target_table}")
print(f"Checkpoint: {checkpoint_dir}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, when, current_timestamp

# =============================================================================
# SOURCE 1: Linux Syslog Events (bronze.host_syslog)
# =============================================================================
# All rows are included — no filtering.  The program field is extracted from
# the syslog message using the standard format:
#   "Mon DD HH:MM:SS hostname program[pid]: message"
# If extraction fails (non-standard format) program is left null.

def transform_syslog(df):
    """Transform bronze.host_syslog rows into silver system events schema."""
    # Standard syslog header: timestamp host program[pid]: rest
    # Capture group 1 = program name (stops at '[' or ':')
    program_col = when(
        F.regexp_extract(
            col("message"),
            r"^\w+\s+\d+\s+[\d:]+\s+\S+\s+(\S+?)[\[:]",
            1
        ) != "",
        F.regexp_extract(
            col("message"),
            r"^\w+\s+\d+\s+[\d:]+\s+\S+\s+(\S+?)[\[:]",
            1
        )
    ).otherwise(lit(None))

    return df.select(
        compute_event_id("host_system_events"),
        col("time"),
        col("device.hostname").alias("hostname"),
        F.coalesce(col("device.os.type"), lit("Linux")).alias("os_type"),
        col("actor.user.name").alias("user"),
        program_col.alias("program"),
        col("message"),
        lit("host_syslog").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# SOURCE 2: Windows System / Application Channel Events
# =============================================================================
# Filter to class_uid=6003 — used in our bronze pipeline for Windows System
# and Application channel events (as opposed to Security channel events).
# The "program" field is stored in metadata.labels.source_type by the bronze
# notebook ("windows_system" or "windows_application").

def transform_windows_system(df):
    """Transform bronze.host_windows_events system/app rows into silver schema."""
    df_sys = df.filter(col("class_uid") == 6003)

    return df_sys.select(
        compute_event_id("host_system_events"),
        col("time"),
        col("device.hostname").alias("hostname"),
        lit("Windows").alias("os_type"),
        col("actor.user.name").alias("user"),
        # source_type label distinguishes "windows_system" from "windows_application"
        col("metadata.labels.source_type").alias("program"),
        col("message"),
        lit("host_windows_events").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# SOURCE 3: Auditd Miscellaneous Events (bronze.host_auditd)
# =============================================================================
# Rows whose audit_type is NOT one of the three specific routing values handled
# by other notebooks (process, auth, account).  These include PATH, CWD,
# CONFIG_CHANGE, NETFILTER_PKT, SYSCALL (non-exec), and other audit record types.
# Using "auditd" as the program since no more-specific field is available.

def transform_auditd_misc(df):
    """Transform bronze.host_auditd non-classified rows into silver system events schema."""
    audit_type = get_audit_type()
    df_misc = df.filter(
        ~audit_type.isin("process", "auth", "account")
    )

    return df_misc.select(
        compute_event_id("host_system_events"),
        col("time"),
        col("device.hostname").alias("hostname"),
        F.coalesce(col("device.os.type"), lit("Linux")).alias("os_type"),
        col("actor.user.name").alias("user"),
        # All remaining auditd records are tagged with the generic "auditd" program
        lit("auditd").alias("program"),
        col("message"),
        lit("host_auditd").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# INGESTION — Delta readStream from all three bronze tables
# =============================================================================

# Source 1: Linux syslog
syslog_source = f"{catalog_name}.bronze.host_syslog"
syslog_checkpoint = f"{checkpoint_dir}/syslog"
print(f"Reading syslog from {syslog_source}...")

try:
    syslog_raw = spark.readStream.table(syslog_source)
    syslog_silver = transform_syslog(syslog_raw)
    (
        syslog_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", syslog_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Syslog done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Syslog skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# Source 2: Windows system/application channel events
windows_source = f"{catalog_name}.bronze.host_windows_events"
windows_checkpoint = f"{checkpoint_dir}/windows"
print(f"Reading Windows system events from {windows_source}...")

try:
    windows_raw = spark.readStream.table(windows_source)
    windows_silver = transform_windows_system(windows_raw)
    (
        windows_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", windows_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Windows system events done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Windows system events skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# Source 3: Auditd miscellaneous events (non-process, non-auth, non-account)
auditd_source = f"{catalog_name}.bronze.host_auditd"
auditd_checkpoint = f"{checkpoint_dir}/auditd"
print(f"Reading auditd misc events from {auditd_source}...")

try:
    auditd_raw = spark.readStream.table(auditd_source)
    auditd_silver = transform_auditd_misc(auditd_raw)
    (
        auditd_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", auditd_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Auditd misc events done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Auditd misc events skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION
# =============================================================================
try_optimize(spark, target_table, ["time", "hostname"])
print(f"Silver host system events complete. Target: {target_table}")
