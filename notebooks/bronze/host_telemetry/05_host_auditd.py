# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Host Auditd Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests Linux auditd log events collected by Cribl Edge from workload
# instances (AWS, Azure, GCP) and writes them to a Delta table in OCSF format.
#
# Auditd events are mapped to OCSF API Activity (class_uid 6003) because
# audit records capture system calls, file access, and process execution —
# all of which are conceptually API-level operations against the kernel.
#
# Status is derived from the _raw field: lines containing "success=yes" map
# to STATUS_SUCCESS, lines containing "success=no" map to STATUS_FAILURE,
# and all others default to STATUS_UNKNOWN. The auditd success field is a
# standard part of the SYSCALL record type.
#
# Source format: JSON under source_type=auditd/
# Target table:  security_poc.bronze.host_auditd
# OCSF class:    API Activity (6003)
# OCSF version:  1.1.0
#
# Parameters (passed via job or widgets):
#   - catalog_name:                      Unity Catalog name (e.g., security_poc)
#   - checkpoint_base:                   Base path for Auto Loader checkpoints
#   - workload_a_host_telemetry_url:     Storage URL for AWS workload A host telemetry
#   - workload_b_host_telemetry_url:     Storage URL for AWS workload B host telemetry
#   - azure_workload_a_host_telemetry_url: Storage URL for Azure workload A host telemetry
#   - gcp_workload_a_host_telemetry_url: Storage URL for GCP workload A host telemetry
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_host_common

# COMMAND ----------

dbutils.widgets.text("catalog_name", "security_poc", "Catalog Name")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")
dbutils.widgets.text("workload_a_host_telemetry_url", "", "AWS Workload A Host Telemetry URL")
dbutils.widgets.text("workload_b_host_telemetry_url", "", "AWS Workload B Host Telemetry URL")
dbutils.widgets.text("azure_workload_a_host_telemetry_url", "", "Azure Workload A Host Telemetry URL")
dbutils.widgets.text("gcp_workload_a_host_telemetry_url", "", "GCP Workload A Host Telemetry URL")

catalog_name = dbutils.widgets.get("catalog_name")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# Collect all non-empty storage URLs — each workload may or may not have host
# telemetry enabled. We skip empty strings to avoid Auto Loader errors on
# nonexistent paths.
_url_params = {
    "workload_a": dbutils.widgets.get("workload_a_host_telemetry_url"),
    "workload_b": dbutils.widgets.get("workload_b_host_telemetry_url"),
    "azure_workload_a": dbutils.widgets.get("azure_workload_a_host_telemetry_url"),
    "gcp_workload_a": dbutils.widgets.get("gcp_workload_a_host_telemetry_url"),
}
storage_urls = {k: v for k, v in _url_params.items() if v.strip()}

# COMMAND ----------

# Build source paths — Cribl Edge partitions host telemetry by source_type.
# Auditd data lives under source_type=auditd/ within each workload's host
# telemetry storage location.
SOURCE_TYPE = "auditd"

source_paths = {
    label: f"{url}source_type={SOURCE_TYPE}/"
    for label, url in storage_urls.items()
}

checkpoint_base_auditd = f"{checkpoint_base}/host_auditd"
target_table = f"{catalog_name}.bronze.host_auditd"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_auditd}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, to_json, struct, lit, when

# =============================================================================
# OCSF TRANSFORMATION — auditd -> OCSF API Activity (6003)
# =============================================================================
# Cribl Edge exports auditd events as JSON with fields including:
#   _raw      — the raw audit log line (key=value pairs)
#   _time     — epoch timestamp (float or int)
#   host      — hostname of the source machine
#   source    — source file path (e.g., /var/log/audit/audit.log)
#   user      — extracted username (if Cribl parser populated it)
#   uid       — numeric user ID from the audit record (if available)
#   auid      — audit UID (login UID, persists across su/sudo)
#   syscall   — system call name or number (for SYSCALL records)
#   type      — audit record type (e.g., SYSCALL, EXECVE, PATH, USER_AUTH)
#   key       — audit rule key that triggered the record (if any)
#
# The auditd _raw field contains key=value pairs like:
#   type=SYSCALL ... success=yes ... uid=0 auid=1000 ...
# We detect success/failure from the "success=yes" / "success=no" pattern.

def transform_auditd_to_ocsf(df):
    """
    Transform raw auditd JSON events into OCSF v1.1.0 API Activity format.
    """
    return df.select(
        # ── Time — Cribl _time is epoch seconds ──
        F.from_unixtime(col("_time")).cast("timestamp").alias("time"),

        # ── Classification — auditd uses API Activity ──
        lit(CLASS_API_ACTIVITY).cast("int").alias("class_uid"),
        lit(CATEGORY_APPLICATION).cast("int").alias("category_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),

        # ── Status — detect success=yes / success=no in _raw ──
        # Auditd SYSCALL records include a "success" field:
        #   success=yes -> the system call completed successfully
        #   success=no  -> the system call failed (e.g., permission denied)
        # Non-SYSCALL records (PATH, CWD, EXECVE, etc.) do not have this field.
        when(
            F.lower(col("_raw")).contains("success=yes"),
            lit(STATUS_SUCCESS)
        ).when(
            F.lower(col("_raw")).contains("success=no"),
            lit(STATUS_FAILURE)
        ).otherwise(
            lit(STATUS_UNKNOWN)
        ).cast("int").alias("status_id"),

        lit(ACTIVITY_UNKNOWN).cast("int").alias("activity_id"),

        # ── Message — the raw auditd log line ──
        col("_raw").alias("message"),

        # ── Actor — the user associated with the audit event ──
        # auditd records may have user, uid, and auid fields. We use the
        # human-readable user name for actor.user.name and the numeric uid
        # for actor.user.uid. The auid (audit login UID) is preserved but
        # not mapped to a standard OCSF field.
        build_actor_struct("user", "uid").alias("actor"),

        # ── Device — the host where the audit event occurred ──
        build_device_struct("host").alias("device"),

        # ── Metadata — product info ──
        build_ocsf_metadata(SOURCE_TYPE).alias("metadata"),

        # ── Source URL — the storage path for provenance tracking ──
        col("_metadata.file_path").alias("src_url"),

        # ── Raw event — original Cribl JSON preserved ──
        to_json(struct(df.columns)).alias("raw_event"),

        # ── Ingestion metadata (project convention, not OCSF) ──
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — process each workload source sequentially
# =============================================================================
# Each workload gets its own Auto Loader stream with a dedicated checkpoint.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_auditd}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        ocsf_df = transform_auditd_to_ocsf(raw_df)

        (
            ocsf_df.writeStream
            .format("delta")
            .outputMode("append")
            .option("checkpointLocation", checkpoint_location)
            .option("mergeSchema", "true")
            .trigger(availableNow=True)
            .toTable(target_table)
        )

        print(f"  {label} done.")
    except Exception as e:
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no files found yet.")
        else:
            raise

print(f"Host auditd ingestion complete. Target: {target_table}")

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION — ZORDER by time and device hostname for
# efficient time-range and per-host queries in downstream analytics.
# =============================================================================
try_optimize(spark, target_table, ["time", "device.hostname"])
