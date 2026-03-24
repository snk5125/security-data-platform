# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Windows Event Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests Windows Security, System, and Application event logs collected by
# Cribl Edge from Windows workload instances (AWS, Azure, GCP) and writes
# them to a single Delta table in OCSF format.
#
# OCSF class routing by source type and EventCode:
#   - windows_security -> Authentication (3002) by default
#     - EventCodes 4720,4722,4723,4724,4725,4726 -> Account Change (3001)
#       (user created, enabled, password changed, reset, disabled, deleted)
#   - windows_system      -> API Activity (6003)
#   - windows_application -> API Activity (6003)
#
# Status is derived from the Keywords field in Windows events:
#   "Audit Success" -> STATUS_SUCCESS
#   "Audit Failure" -> STATUS_FAILURE
#   Otherwise       -> STATUS_UNKNOWN
#
# This notebook iterates all 3 Windows source types across all workload
# storage URLs, writing to a single target table.
#
# Source format: JSON under source_type=windows_security/,
#               source_type=windows_system/, source_type=windows_application/
# Target table:  security_poc.bronze.host_windows_events
# OCSF classes:  Authentication (3002), Account Change (3001), API Activity (6003)
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

# =============================================================================
# WINDOWS SOURCE TYPES — three distinct event log channels
# =============================================================================
# Each Windows event log channel is stored under its own source_type partition
# by Cribl Edge. We iterate all three and write to a single target table.
WINDOWS_SOURCE_TYPES = [
    "windows_security",
    "windows_system",
    "windows_application",
]

# Account-change EventCodes — these specific Security event IDs indicate
# user account lifecycle changes and route to OCSF Account Change (3001)
# instead of the default Authentication (3002) for security events.
#   4720 — A user account was created
#   4722 — A user account was enabled
#   4723 — An attempt was made to change an account's password
#   4724 — An attempt was made to reset an account's password
#   4725 — A user account was disabled
#   4726 — A user account was deleted
ACCOUNT_CHANGE_EVENT_CODES = {4720, 4722, 4723, 4724, 4725, 4726}

checkpoint_base_win = f"{checkpoint_base}/host_windows_events"
target_table = f"{catalog_name}.bronze.host_windows_events"

print(f"Windows source types: {WINDOWS_SOURCE_TYPES}")
print(f"Storage URLs: {list(storage_urls.keys())}")
print(f"Checkpoint:   {checkpoint_base_win}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, to_json, struct, lit, when

# =============================================================================
# CLASS UID ROUTING — determines the OCSF class for each Windows source type
# =============================================================================

def get_class_uid_for_source(source_type):
    """
    Return the default OCSF class_uid for a given Windows event log source type.
    windows_security events default to Authentication (3002) — with EventCode-
    based overrides applied later for account-change events. System and
    Application events use the generic API Activity (6003).
    """
    if source_type == "windows_security":
        return CLASS_AUTHENTICATION
    return CLASS_API_ACTIVITY


def get_category_uid_for_class(class_uid):
    """
    Return the OCSF category_uid for a given class_uid.
    Authentication (3002) and Account Change (3001) -> IAM (3)
    API Activity (6003) -> Application (6)
    """
    if class_uid in (CLASS_AUTHENTICATION, CLASS_ACCOUNT_CHANGE):
        return CATEGORY_IAM
    return CATEGORY_APPLICATION

# COMMAND ----------

# =============================================================================
# OCSF TRANSFORMATION — Windows Event Logs -> OCSF Authentication / Account
#                                              Change / API Activity
# =============================================================================
# Cribl Edge exports Windows event logs as JSON with fields including:
#   _raw         — the raw event XML or rendered text
#   _time        — epoch timestamp (float or int)
#   host         — hostname of the source machine
#   source       — Windows event log channel (Security, System, Application)
#   EventCode    — Windows event ID (numeric)
#   Keywords     — audit keyword string (e.g., "Audit Success", "Audit Failure")
#   ComputerName — FQDN of the Windows host
#   User         — the Windows account associated with the event (if available)
#   Sid          — Security Identifier of the user (if available)
#   Message      — rendered event message (if available)
#
# For windows_security events, we apply EventCode-based routing:
#   - Account-change codes (4720-4726) -> Account Change (3001)
#   - All other security events -> Authentication (3002)

def transform_windows_to_ocsf(df, source_type):
    """
    Transform raw Windows event log JSON events into OCSF v1.1.0 format.

    Args:
        df:          The raw DataFrame from Auto Loader.
        source_type: One of "windows_security", "windows_system",
                     "windows_application" — determines OCSF class routing.
    """
    default_class = get_class_uid_for_source(source_type)
    default_category = get_category_uid_for_class(default_class)

    # For security events, apply EventCode-based overrides for account changes.
    # For other source types, all events get the default class.
    acct_change_codes = list(ACCOUNT_CHANGE_EVENT_CODES)

    if source_type == "windows_security":
        class_uid_col = (
            when(col("EventCode").cast("int").isin(acct_change_codes), lit(CLASS_ACCOUNT_CHANGE))
            .otherwise(lit(default_class))
            .cast("int")
        )
        category_uid_col = (
            when(col("EventCode").cast("int").isin(acct_change_codes), lit(CATEGORY_IAM))
            .otherwise(lit(default_category))
            .cast("int")
        )
    else:
        class_uid_col = lit(default_class).cast("int")
        category_uid_col = lit(default_category).cast("int")

    return df.select(
        # ── Time — Cribl _time is epoch seconds ──
        F.from_unixtime(col("_time")).cast("timestamp").alias("time"),

        # ── Classification ──
        class_uid_col.alias("class_uid"),
        category_uid_col.alias("category_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),

        # ── Status — detect Audit Success/Failure from Keywords field ──
        # Windows Security events set Keywords to "Audit Success" or
        # "Audit Failure". System/Application events may not have this field.
        when(
            F.lower(F.coalesce(col("Keywords"), lit(""))).contains("audit success"),
            lit(STATUS_SUCCESS)
        ).when(
            F.lower(F.coalesce(col("Keywords"), lit(""))).contains("audit failure"),
            lit(STATUS_FAILURE)
        ).otherwise(
            lit(STATUS_UNKNOWN)
        ).cast("int").alias("status_id"),

        lit(ACTIVITY_UNKNOWN).cast("int").alias("activity_id"),

        # ── Message — rendered event message or raw event ──
        F.coalesce(col("Message"), col("_raw")).alias("message"),

        # ── Actor — the Windows user account associated with the event ──
        # Windows events may have User and/or Sid fields from Cribl parsing.
        F.struct(
            F.struct(
                F.coalesce(col("User"), lit(None).cast("string")).alias("name"),
                F.coalesce(col("Sid"), lit(None).cast("string")).alias("uid"),
            ).alias("user"),
        ).alias("actor"),

        # ── Device — the Windows host ──
        # Use ComputerName if available, fall back to host.
        build_device_struct(
            hostname_col="host",
            os_name="Windows Server 2022",
            os_type="Windows",
        ).alias("device"),

        # ── Metadata — product info with source type label ──
        build_ocsf_metadata(source_type).alias("metadata"),

        # ── Source URL — the storage path for provenance tracking ──
        col("_metadata.file_path").alias("src_url"),

        # ── Raw event — original Cribl JSON preserved ──
        to_json(struct(df.columns)).alias("raw_event"),

        # ── Ingestion metadata (project convention, not OCSF) ──
        current_timestamp().alias("_ingested_at"),
    )

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — iterate all 3 Windows source types x all workload URLs
# =============================================================================
# Each (source_type, workload) combination gets its own Auto Loader stream
# with a dedicated checkpoint. This ensures that adding a new workload or
# source type does not re-process existing data.

for source_type in WINDOWS_SOURCE_TYPES:
    for label, base_url in storage_urls.items():
        path = f"{base_url}source_type={source_type}/"
        checkpoint_location = f"{checkpoint_base_win}/{source_type}/{label}"
        print(f"Ingesting {source_type}/{label} from {path} ...")

        try:
            raw_df = (
                spark.readStream.format("cloudFiles")
                .option("cloudFiles.format", "json")
                .option("cloudFiles.inferColumnTypes", "true")
                .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
                .option("cloudFiles.schemaLocation", checkpoint_location)
                .load(path)
            )

            ocsf_df = transform_windows_to_ocsf(raw_df, source_type)

            (
                ocsf_df.writeStream
                .format("delta")
                .outputMode("append")
                .option("checkpointLocation", checkpoint_location)
                .option("mergeSchema", "true")
                .trigger(availableNow=True)
                .toTable(target_table)
            )

            print(f"  {source_type}/{label} done.")
        except Exception as e:
            if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
                print(f"  {source_type}/{label} skipped — no files found yet.")
            else:
                raise

print(f"Windows event log ingestion complete. Target: {target_table}")

# COMMAND ----------

# =============================================================================
# POST-INGESTION OPTIMIZATION — ZORDER by time, class_uid, and device hostname
# for efficient time-range, event-type, and per-host queries.
# =============================================================================
try_optimize(spark, target_table, ["time", "class_uid", "device.hostname"])
