# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Layer: Host Process Executions (Cross-Platform)
# -----------------------------------------------------------------------------
# Normalizes process execution events from Linux (host_commands, host_auditd)
# and Windows (host_windows_events) bronze tables into a unified silver table.
#
# Linux bash history: all recorded commands from ~/.bash_history or auditd hooks
# Linux auditd execve: process exec events with pid/ppid extracted from audit log
# Windows process creation: EventCode 4688 (New Process Created)
#
# Source tables:  security_poc.bronze.host_commands      (bash_history)
#                 security_poc.bronze.host_auditd         (auditd_execve, filter type=process)
#                 security_poc.bronze.host_windows_events (process, filter EventCode 4688)
# Target table:   security_poc.silver.host_process_executions
# Schedule:       Every 15 min (Delta readStream with availableNow)
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_silver_host_common

# COMMAND ----------

dbutils.widgets.text("catalog_name", "security_poc", "Catalog Name")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

catalog_name = dbutils.widgets.get("catalog_name")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

target_table = f"{catalog_name}.silver.host_process_executions"
checkpoint_dir = f"{checkpoint_base}/silver_host_processes"

print(f"Target: {target_table}")
print(f"Checkpoint: {checkpoint_dir}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, when, current_timestamp

# =============================================================================
# SOURCE 1: Bash History Events (bronze.host_commands)
# =============================================================================
# All rows are bash command executions — no filtering needed.
# bash_history doesn't carry success/failure signal, so status = "unknown".
# parent_process and pid are not available from this source.

def transform_bash_history(df):
    """Transform bronze.host_commands rows into silver process schema."""
    # Parse the OS username from the source file path in raw_event.
    # The source field contains the .bash_history path, e.g.:
    #   /home/azureadmin/.bash_history  -> "azureadmin"
    #   /home/ec2-user/.bash_history    -> "ec2-user"
    #   /root/.bash_history             -> "root"
    # The actor.user.name field contains the Databricks job runner identity
    # (e.g. sethdemoa@gmail.com) and is NOT the OS user.
    # The source file path is embedded in raw_event JSON as "source":"/home/user/.bash_history"
    source_path = F.get_json_object(col("raw_event"), "$.source")
    parsed_user = (
        when(
            F.regexp_extract(source_path, r"/home/([^/]+)/\.bash_history", 1) != "",
            F.regexp_extract(source_path, r"/home/([^/]+)/\.bash_history", 1)
        ).when(
            source_path.contains("/root/.bash_history"),
            lit("root")
        ).otherwise(col("actor.user.name"))
    )

    return df.select(
        compute_event_id("host_process_executions"),
        col("time"),
        col("device.hostname").alias("hostname"),
        # bash_history is Linux-only; fall back to "Linux" if os.type is absent
        F.coalesce(col("device.os.type"), lit("Linux")).alias("os_type"),
        parsed_user.alias("user"),

        # The entire command line is stored in message for bash_history
        col("message").alias("command_line"),

        # bash_history carries no parent process context
        lit(None).cast("string").alias("parent_process"),
        lit(None).cast("int").alias("pid"),

        lit("bash_history").alias("source_type"),

        # bash_history does not indicate whether the command succeeded
        lit("unknown").alias("status"),

        lit("host_commands").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# SOURCE 2: Auditd Execve Events (bronze.host_auditd, audit_type=process)
# =============================================================================
# Filter to EXECVE/SYSCALL records using the shared get_audit_type() helper.
# Extract exe path from audit message (exe="..." or exe=<path>) and parse
# pid/ppid for process tree reconstruction.

def transform_auditd_process(df):
    """Transform bronze.host_auditd process rows into silver process schema."""
    df_proc = df.filter(get_audit_type() == "process")

    return df_proc.select(
        compute_event_id("host_process_executions"),
        col("time"),
        col("device.hostname").alias("hostname"),
        F.coalesce(col("device.os.type"), lit("Linux")).alias("os_type"),
        col("actor.user.name").alias("user"),

        # Extract executable path — prefer quoted form exe="...", then unquoted exe=<path>
        when(
            F.regexp_extract(col("message"), r'exe="([^"]+)"', 1) != "",
            F.regexp_extract(col("message"), r'exe="([^"]+)"', 1)
        ).otherwise(
            F.regexp_extract(col("message"), r'exe=(\S+)', 1)
        ).alias("command_line"),

        # Parent process ID from ppid=<N> — store as string path-like value
        when(
            F.regexp_extract(col("message"), r"ppid=(\d+)", 1) != "",
            F.regexp_extract(col("message"), r"ppid=(\d+)", 1)
        ).otherwise(lit(None)).alias("parent_process"),

        # PID from pid=<N>
        when(
            F.regexp_extract(col("message"), r"\bpid=(\d+)", 1) != "",
            F.regexp_extract(col("message"), r"\bpid=(\d+)", 1).cast("int")
        ).otherwise(lit(None).cast("int")).alias("pid"),

        lit("auditd_execve").alias("source_type"),

        # Map bronze status_id to human-readable string
        when(col("status_id") == 1, lit("success"))
        .when(col("status_id") == 2, lit("failure"))
        .otherwise(lit("unknown")).alias("status"),

        lit("host_auditd").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# SOURCE 3: Windows Process Creation Events (bronze.host_windows_events)
# =============================================================================
# Filter to process-creation records via get_win_event_type() or EventCode 4688.
# Extract process path from "New Process Name:" and parent from
# "Creator Process Name:" in the message.  The full command line is often
# masked by Windows audit policy, so we record the process path only.
# PID is in hex (e.g., "New Process ID: 0x1a3c") and must be converted.

def transform_windows_process(df):
    """Transform bronze.host_windows_events process rows into silver process schema."""
    win_type = get_win_event_type()

    # Accept rows tagged as "process" by the label, OR rows with EventCode 4688
    event_code_col = when(
        F.regexp_extract(col("message"), r"EventCode=(\d+)", 1) != "",
        F.regexp_extract(col("message"), r"EventCode=(\d+)", 1).cast("int")
    )
    df_proc = df.filter(
        (win_type == "process") |
        (event_code_col == 4688)
    )

    # Hex PID string from "New Process ID: 0x<hex>"
    hex_pid = F.regexp_extract(col("message"), r"New Process ID:\s+0x([0-9a-fA-F]+)", 1)

    return df_proc.select(
        compute_event_id("host_process_executions"),
        col("time"),
        col("device.hostname").alias("hostname"),
        lit("Windows").alias("os_type"),
        col("actor.user.name").alias("user"),

        # New Process Name field carries the full executable path
        F.regexp_extract(col("message"), r"New Process Name:\s+(\S+)", 1).alias("command_line"),

        # Creator process name as the parent identifier
        when(
            F.regexp_extract(col("message"), r"Creator Process Name:\s+(\S+)", 1) != "",
            F.regexp_extract(col("message"), r"Creator Process Name:\s+(\S+)", 1)
        ).otherwise(lit(None)).alias("parent_process"),

        # Convert hex PID to int; null if pattern not matched
        when(hex_pid != "", F.conv(hex_pid, 16, 10).cast("int"))
        .otherwise(lit(None).cast("int")).alias("pid"),

        lit("windows_process_creation").alias("source_type"),

        when(col("status_id") == 1, lit("success"))
        .when(col("status_id") == 2, lit("failure"))
        .otherwise(lit("unknown")).alias("status"),

        lit("host_windows_events").alias("source_table"),
        col("raw_event"),
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# INGESTION — Delta readStream from all three bronze tables
# =============================================================================

# Source 1: Bash history
bash_source = f"{catalog_name}.bronze.host_commands"
bash_checkpoint = f"{checkpoint_dir}/bash_history"
print(f"Reading bash history from {bash_source}...")

try:
    bash_raw = spark.readStream.table(bash_source)
    bash_silver = transform_bash_history(bash_raw)
    (
        bash_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", bash_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Bash history done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Bash history skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# Source 2: Auditd process executions
auditd_source = f"{catalog_name}.bronze.host_auditd"
auditd_checkpoint = f"{checkpoint_dir}/auditd"
print(f"Reading auditd process events from {auditd_source}...")

try:
    auditd_raw = spark.readStream.table(auditd_source)
    auditd_silver = transform_auditd_process(auditd_raw)
    (
        auditd_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", auditd_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Auditd process events done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Auditd process events skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# Source 3: Windows process creation (EventCode 4688)
windows_source = f"{catalog_name}.bronze.host_windows_events"
windows_checkpoint = f"{checkpoint_dir}/windows"
print(f"Reading Windows process events from {windows_source}...")

try:
    windows_raw = spark.readStream.table(windows_source)
    windows_silver = transform_windows_process(windows_raw)
    (
        windows_silver.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", windows_checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target_table)
    )
    print(f"  Windows process events done.")
except Exception as e:
    err = str(e)
    if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
        print(f"  Windows process events skipped — bronze table not found yet.")
    else:
        raise

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION
# =============================================================================
try_optimize(spark, target_table, ["time", "hostname"])
print(f"Silver host process executions complete. Target: {target_table}")
