# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Host Syslog Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests syslog / messages log events collected by Cribl Edge from workload
# instances (AWS, Azure, GCP) and writes them to a Delta table in OCSF format.
#
# Syslog entries are mapped to OCSF API Activity (class_uid 6003) because
# syslog captures a wide variety of system operations that don't fit neatly
# into a more specific OCSF class. Status defaults to STATUS_UNKNOWN since
# syslog messages do not consistently indicate success or failure.
#
# Source format: JSON under source_type=syslog/
# Target table:  security_poc.bronze.host_syslog
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
# Syslog data lives under source_type=syslog/ within each workload's host
# telemetry storage location.
SOURCE_TYPE = "syslog"

source_paths = {
    label: f"{url}source_type={SOURCE_TYPE}/"
    for label, url in storage_urls.items()
}

checkpoint_base_syslog = f"{checkpoint_base}/host_syslog"
target_table = f"{catalog_name}.bronze.host_syslog"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_syslog}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, to_json, struct, lit

# =============================================================================
# OCSF TRANSFORMATION — syslog / messages -> OCSF API Activity (6003)
# =============================================================================
# Cribl Edge exports syslog events as JSON with fields including:
#   _raw      — the raw syslog line
#   _time     — epoch timestamp (float or int)
#   host      — hostname of the source machine
#   source    — source file path (e.g., /var/log/syslog, /var/log/messages)
#   facility  — syslog facility (if parsed)
#   severity  — syslog severity string (if parsed)
#   program   — the program that generated the log
#   pid       — process ID (if available)
#
# Syslog messages are highly varied (kernel, cron, systemd, application logs)
# so we use the generic API Activity class and preserve the full message for
# downstream silver-layer parsing.

def transform_syslog_to_ocsf(df):
    """
    Transform raw syslog JSON events into OCSF v1.1.0 API Activity format.
    """
    return df.select(
        # ── Time — Cribl _time is epoch seconds ──
        F.from_unixtime(col("_time")).cast("timestamp").alias("time"),

        # ── Classification — syslog uses generic API Activity ──
        lit(CLASS_API_ACTIVITY).cast("int").alias("class_uid"),
        lit(CATEGORY_APPLICATION).cast("int").alias("category_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),

        # ── Status — syslog does not consistently indicate success/failure ──
        lit(STATUS_UNKNOWN).cast("int").alias("status_id"),

        lit(ACTIVITY_UNKNOWN).cast("int").alias("activity_id"),

        # ── Message — the raw syslog line ──
        col("_raw").alias("message"),

        # ── Actor — syslog does not always have a clear user; use "system" ──
        # If a 'user' field exists from Cribl parsing, use it; otherwise null.
        F.struct(
            F.struct(
                F.coalesce(col("user"), lit(None).cast("string")).alias("name"),
                F.lit(None).cast("string").alias("uid"),
            ).alias("user"),
        ).alias("actor"),

        # ── Device — the host where the syslog event occurred ──
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
    checkpoint_location = f"{checkpoint_base_syslog}/{label}"
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

        ocsf_df = transform_syslog_to_ocsf(raw_df)

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
        err = str(e)
        if "CF_EMPTY_DIR" in err or "empty" in err.lower() or "FileNotFoundException" in err or "No such file or directory" in err:
            print(f"  {label} skipped — no data available yet.")
        else:
            raise

print(f"Host syslog ingestion complete. Target: {target_table}")

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION — ZORDER by time and device hostname for
# efficient time-range and per-host queries in downstream analytics.
# =============================================================================
try_optimize(spark, target_table, ["time", "device.hostname"])
