# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: GCP Cloud Audit Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests GCP Cloud Audit Logs from workload project GCS storage and writes
# them to a Delta table in OCSF format. Each event is routed to the appropriate
# OCSF event class based on the protoPayload.methodName:
#
#   - API Activity (6003)     — default for resource operations
#   - Authentication (3002)   — token generation, login, JWT signing events
#   - Account Change (3001)   — IAM policy changes, role mutations, SA changes
#
# All three classes are stored in a single table with class_uid as a column,
# matching the pattern used by the AWS CloudTrail and Azure Activity Log
# notebooks. The original audit log event is preserved in the raw_data field
# for forensic/audit use.
#
# Cloud Audit Logs exported via Cloud Logging sinks arrive as JSON files with
# a Cloud Logging envelope (logName, resource, timestamp, protoPayload, etc.).
# The protoPayload contains the actual audit log entry (AuditLog type).
#
# Source format: JSON under cloudaudit.googleapis.com/
# Target table: security_poc.bronze.gcp_audit_log_raw
# OCSF version: 1.1.0
#
# Parameters (passed via job or widgets):
#   - gcp_workload_a_storage_url: Storage URL for GCP workload A
#                                 (e.g. gs://bucket-name/)
#   - checkpoint_base:            Path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_gcp_common

# COMMAND ----------

dbutils.widgets.text("gcp_workload_a_storage_url", "", "GCP Workload A Storage URL")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

gcp_workload_a_storage_url = dbutils.widgets.get("gcp_workload_a_storage_url")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source path — Cloud Audit Logs exported via Cloud Logging sinks are written
# under the cloudaudit.googleapis.com/ prefix. storage_url already includes
# the scheme and trailing slash (e.g. "gs://bucket-name/"), so path suffixes
# are appended directly.
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}cloudaudit.googleapis.com/",
}

checkpoint_base_cal = f"{checkpoint_base}/gcp_audit_log"
target_table = "security_poc.bronze.gcp_audit_log_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_cal}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, current_timestamp, to_json, struct, lit, when, array, upper
)

# =============================================================================
# OCSF CLASS ROUTING — determines which OCSF class an audit log event maps to
# based on protoPayload.methodName
# =============================================================================

def _is_auth_event(method_name_col):
    """
    Returns a boolean column that is True when the event should be classified
    as an Authentication event (OCSF class 3002).

    Checks if methodName contains any of the AUTH_METHOD_PATTERNS substrings.
    """
    condition = F.lit(False)
    for pattern in AUTH_METHOD_PATTERNS:
        condition = condition | method_name_col.contains(pattern)
    return condition


def _is_account_change_event(method_name_col):
    """
    Returns a boolean column that is True when the event should be classified
    as an Account Change event (OCSF class 3001).

    Checks if methodName contains any of the ACCOUNT_CHANGE_METHOD_PATTERNS
    substrings (IAM policy changes, role mutations, service account changes).
    """
    condition = F.lit(False)
    for pattern in ACCOUNT_CHANGE_METHOD_PATTERNS:
        condition = condition | method_name_col.contains(pattern)
    return condition

# COMMAND ----------

# =============================================================================
# OCSF TRANSFORMATION — Cloud Audit Logs -> OCSF API Activity / Authentication
#                                            / Account Change
# =============================================================================
# Reads the Auto Loader inferred schema, classifies each event into an OCSF
# class, then maps fields to the OCSF structure. The full original event is
# preserved as a JSON string in raw_data.

def transform_audit_log_to_ocsf(df):
    """
    Transform a raw GCP Cloud Audit Log DataFrame (one row per event) into
    OCSF v1.1.0 format.
    """

    # ── Step 1: Route to OCSF class based on protoPayload.methodName ─────
    # Cloud Audit Logs store the method name in protoPayload.methodName.
    # We extract it first to simplify classification.
    df_with_method = df.withColumn(
        "_method_name",
        F.coalesce(col("protoPayload.methodName"), lit(""))
    )

    df_classified = df_with_method.withColumn(
        "class_uid",
        when(
            _is_auth_event(col("_method_name")),
            lit(CLASS_AUTHENTICATION)
        )
        .when(
            _is_account_change_event(col("_method_name")),
            lit(CLASS_ACCOUNT_CHANGE)
        )
        .otherwise(lit(CLASS_API_ACTIVITY))
        .cast("int")
    ).withColumn(
        "category_uid",
        when(col("class_uid") == CLASS_AUTHENTICATION, lit(CATEGORY_IAM))
        .when(col("class_uid") == CLASS_ACCOUNT_CHANGE, lit(CATEGORY_IAM))
        .otherwise(lit(CATEGORY_APPLICATION))
        .cast("int")
    )

    # ── Step 2: Derive activity_id and status ────────────────────────────
    # Cloud Audit Logs use protoPayload.status.code for success/failure.
    # A code of 0 (or absent) means success; any other value means failure.
    df_activity = df_classified.withColumn(
        "activity_id", gcp_activity_id(col("_method_name"))
    ).withColumn(
        "status_id",
        when(
            F.coalesce(col("protoPayload.status.code"), lit(0)) == 0,
            lit(STATUS_SUCCESS)
        )
        .otherwise(lit(STATUS_FAILURE))
        .cast("int")
    ).withColumn(
        "status",
        when(
            F.coalesce(col("protoPayload.status.code"), lit(0)) == 0,
            lit("Success")
        )
        .otherwise(lit("Failure"))
    )

    # ── Step 3: Extract project ID from resource.labels ──────────────────
    # Cloud Logging entries include resource.labels.project_id for the
    # project where the event occurred.
    df_with_project = df_activity.withColumn(
        "_project_id",
        F.coalesce(col("resource.labels.project_id"), lit("unknown"))
    )

    # ── Step 4: Build OCSF columns ──────────────────────────────────────
    df_ocsf = df_with_project.select(
        # ── Classification ──
        col("class_uid"),
        col("category_uid"),
        col("activity_id"),
        compute_type_uid(col("class_uid"), col("activity_id")).alias("type_uid"),
        gcp_severity_to_ocsf(col("severity")).alias("severity_id"),
        severity_label(gcp_severity_to_ocsf(col("severity"))).alias("severity"),

        # ── Time ──
        # Cloud Logging timestamp is ISO8601 string — cast to timestamp.
        # Fall back to receiveTimestamp if timestamp is missing.
        F.coalesce(
            col("timestamp").cast("timestamp"),
            col("receiveTimestamp").cast("timestamp"),
        ).alias("time"),

        # ── Status ──
        col("status_id"),
        col("status"),
        col("protoPayload.status.code").cast("string").alias("status_code"),
        col("protoPayload.status.message").alias("status_detail"),

        # ── API object — the core of Cloud Audit Log mapping ──
        F.struct(
            col("_method_name").alias("operation"),
            F.struct(
                col("protoPayload.serviceName").alias("name"),
            ).alias("service"),
        ).alias("api"),

        # ── Actor — who performed the action ──
        # Cloud Audit Logs use protoPayload.authenticationInfo for the caller
        # identity. principalEmail is the primary identifier.
        F.struct(
            F.struct(
                col("protoPayload.authenticationInfo.principalEmail").alias("name"),
                col("protoPayload.authenticationInfo.principalEmail").alias("uid"),
                # Determine type: if email contains gserviceaccount.com -> ServiceAccount
                when(
                    col("protoPayload.authenticationInfo.principalEmail").contains("gserviceaccount.com"),
                    lit("ServiceAccount")
                ).otherwise(lit("User")).alias("type"),
            ).alias("user"),
        ).alias("actor"),

        # ── Resource — the target of the operation ──
        # Parse the GCP resource name into structured components when available.
        when(
            col("protoPayload.resourceName").isNotNull(),
            parse_gcp_resource_name(col("protoPayload.resourceName"))
        ).alias("resource"),

        # ── Cloud context ──
        # Use resource.labels.location or resource.labels.zone for region;
        # fall back to "global" if not present.
        ocsf_cloud(
            F.coalesce(
                col("resource.labels.location"),
                col("resource.labels.zone"),
                lit("global"),
            ),
            col("_project_id"),
        ).alias("cloud"),

        # ── Metadata ──
        ocsf_metadata("Cloud Audit Logs", "Cloud Audit Logs").alias("metadata"),

        # ── Unmapped — fields that don't fit OCSF cleanly ──
        # protoPayload.request and protoPayload.response are deeply nested
        # JSON blobs that vary per API call. Storing them as JSON strings
        # in unmapped preserves the data without imposing a schema.
        F.map_from_arrays(
            array(
                lit("request"),
                lit("response"),
                lit("log_name"),
                lit("resource_name"),
                lit("resource_type"),
                lit("insert_id"),
                lit("authorization_info"),
            ),
            array(
                to_json(col("protoPayload.request")).cast("string"),
                to_json(col("protoPayload.response")).cast("string"),
                col("logName").cast("string"),
                col("protoPayload.resourceName").cast("string"),
                col("resource.type").cast("string"),
                col("insertId").cast("string"),
                to_json(col("protoPayload.authorizationInfo")).cast("string"),
            ),
        ).alias("unmapped"),

        # ── Raw data — complete original event as JSON ──
        to_json(struct(df.columns)).alias("raw_data"),

        # ── Ingestion metadata (project convention, not OCSF) ──
        current_timestamp().alias("_ingested_at"),
        col("_metadata.file_path").alias("_source_file"),
    )

    return df_ocsf

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — process each workload source sequentially
# =============================================================================
# Each source gets its own Auto Loader stream with a dedicated checkpoint.
# Auto Loader reads raw Cloud Audit Log JSON, then the OCSF transformation is
# applied. Cloud Logging sink exports write JSON files with one log entry per
# line or as JSON arrays depending on sink configuration.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_cal}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read raw Cloud Audit Log JSON with schema inference.
        # schemaHints ensures protoPayload.status is present in the schema
        # even when the initial batch has no failed requests (status is only
        # included in audit log entries when an error occurred).
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .option("cloudFiles.schemaHints", "protoPayload.status STRUCT<code: INT, message: STRING>, resource.labels.location STRING")
            .load(path)
        )

        # Apply OCSF transformation and write to Delta.
        ocsf_df = transform_audit_log_to_ocsf(raw_df)

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

print(f"Cloud Audit Log ingestion complete. Rows: {spark.table(target_table).count()}")
