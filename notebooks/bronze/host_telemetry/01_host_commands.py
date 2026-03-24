# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Host Command History Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests .bash_history command logs collected by Cribl Edge from workload
# instances (AWS, Azure, GCP) and writes them to a Delta table in OCSF format.
#
# Each command is mapped to OCSF API Activity (class_uid 6003) because shell
# commands are conceptually API calls against the operating system. The metadata
# includes obfuscated=true because Cribl Edge may have masked sensitive command
# arguments (passwords, tokens, etc.) before forwarding.
#
# Source format: JSON under source_type=bash_history/
# Target table:  security_poc.bronze.host_commands
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
# bash_history data lives under source_type=bash_history/ within each
# workload's host telemetry storage location.
SOURCE_TYPE = "bash_history"

source_paths = {
    label: f"{url}source_type={SOURCE_TYPE}/"
    for label, url in storage_urls.items()
}

checkpoint_base_cmd = f"{checkpoint_base}/host_commands"
target_table = f"{catalog_name}.bronze.host_commands"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_cmd}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, to_json, struct, lit, when

# =============================================================================
# OCSF TRANSFORMATION — bash_history -> OCSF API Activity (6003)
# =============================================================================
# Cribl Edge exports bash_history events as JSON with fields including:
#   _raw      — the command string
#   _time     — epoch timestamp (float or int)
#   host      — hostname of the source machine
#   source    — source file path (e.g., /home/user/.bash_history)
#   user      — the user who executed the command (if available)
#   index     — Cribl index/source label
#
# We map these to the OCSF API Activity schema, treating each command as
# an "operation" invocation on the host OS.

def transform_commands_to_ocsf(df):
    """
    Transform raw bash_history JSON events into OCSF v1.1.0 API Activity format.
    """
    return df.select(
        # ── Time — Cribl _time is epoch seconds ──
        F.from_unixtime(col("_time")).cast("timestamp").alias("time"),

        # ── Classification — all commands are API Activity ──
        lit(CLASS_API_ACTIVITY).cast("int").alias("class_uid"),
        lit(CATEGORY_APPLICATION).cast("int").alias("category_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),
        lit(STATUS_SUCCESS).cast("int").alias("status_id"),
        lit(ACTIVITY_CREATE).cast("int").alias("activity_id"),

        # ── Message — the command itself ──
        col("_raw").alias("message"),

        # ── Actor — the user who ran the command ──
        # Cribl may populate a 'user' field from the bash_history file path
        # (e.g., /home/ubuntu/.bash_history -> user=ubuntu). Fall back to
        # extracting from the source path if the user field is absent.
        build_actor_struct("user").alias("actor"),

        # ── Device — the host where the command was executed ──
        build_device_struct("host").alias("device"),

        # ── Metadata — product info with obfuscated flag ──
        # obfuscated=True because Cribl Edge may mask sensitive arguments
        build_ocsf_metadata(SOURCE_TYPE, obfuscated=True).alias("metadata"),

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
# Auto Loader reads raw JSON from the Cribl Edge output path, then the OCSF
# transformation is applied.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_cmd}/{label}"
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

        ocsf_df = transform_commands_to_ocsf(raw_df)

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

print(f"Host commands ingestion complete. Target: {target_table}")

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION — ZORDER by time and device hostname for
# efficient time-range and per-host queries in downstream analytics.
# =============================================================================
try_optimize(spark, target_table, ["time", "device.hostname"])
