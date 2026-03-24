# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Host Authentication Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests auth.log / secure log events collected by Cribl Edge from workload
# instances (AWS, Azure, GCP) and writes them to a Delta table in OCSF format.
#
# Each authentication event is mapped to OCSF Authentication (class_uid 3002).
# Status is derived from the _raw field: lines containing "Accepted" map to
# STATUS_SUCCESS, lines containing "Failed" map to STATUS_FAILURE, and all
# others default to STATUS_UNKNOWN.
#
# Source format: JSON under source_type=auth/
# Target table:  security_poc.bronze.host_auth
# OCSF class:    Authentication (3002)
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
# Auth log data lives under source_type=auth/ within each workload's host
# telemetry storage location.
SOURCE_TYPE = "auth"

source_paths = {
    label: f"{url}source_type={SOURCE_TYPE}/"
    for label, url in storage_urls.items()
}

checkpoint_base_auth = f"{checkpoint_base}/host_auth"
target_table = f"{catalog_name}.bronze.host_auth"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_auth}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, to_json, struct, lit, when

# =============================================================================
# OCSF TRANSFORMATION — auth.log / secure -> OCSF Authentication (3002)
# =============================================================================
# Cribl Edge exports auth log events as JSON with fields including:
#   _raw      — the raw syslog line (e.g., "Accepted publickey for ubuntu...")
#   _time     — epoch timestamp (float or int)
#   host      — hostname of the source machine
#   source    — source file path (e.g., /var/log/auth.log)
#   user      — extracted username (if Cribl parser populated it)
#   pid       — process ID (if available)
#   program   — the program that generated the log (e.g., sshd, sudo)
#
# We detect authentication success/failure by searching for "Accepted" or
# "Failed" keywords in the _raw field, matching the standard OpenSSH and
# PAM log patterns.

def transform_auth_to_ocsf(df):
    """
    Transform raw auth log JSON events into OCSF v1.1.0 Authentication format.
    """
    return df.select(
        # ── Time — Cribl _time is epoch seconds ──
        F.from_unixtime(col("_time")).cast("timestamp").alias("time"),

        # ── Classification — all auth logs are Authentication events ──
        lit(CLASS_AUTHENTICATION).cast("int").alias("class_uid"),
        lit(CATEGORY_IAM).cast("int").alias("category_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),

        # ── Status — detect Accepted/Failed in _raw for success/failure ──
        # OpenSSH logs: "Accepted publickey for ...", "Failed password for ..."
        # PAM/sudo logs: "session opened", "authentication failure"
        when(
            F.lower(col("_raw")).contains("accepted")
            | F.lower(col("_raw")).contains("session opened"),
            lit(STATUS_SUCCESS)
        ).when(
            F.lower(col("_raw")).contains("failed")
            | F.lower(col("_raw")).contains("authentication failure")
            | F.lower(col("_raw")).contains("invalid user"),
            lit(STATUS_FAILURE)
        ).otherwise(
            lit(STATUS_UNKNOWN)
        ).cast("int").alias("status_id"),

        lit(ACTIVITY_UNKNOWN).cast("int").alias("activity_id"),

        # ── Message — the raw auth log line ──
        col("_raw").alias("message"),

        # ── Actor — the user attempting to authenticate ──
        build_actor_struct("user").alias("actor"),

        # ── Device — the host where authentication occurred ──
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
    checkpoint_location = f"{checkpoint_base_auth}/{label}"
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

        ocsf_df = transform_auth_to_ocsf(raw_df)

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

print(f"Host auth ingestion complete. Target: {target_table}")

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION — ZORDER by time and device hostname for
# efficient time-range and per-host queries in downstream analytics.
# =============================================================================
try_optimize(spark, target_table, ["time", "device.hostname"])
