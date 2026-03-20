# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Azure Activity Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests Azure Activity Log events from workload account storage and writes
# them to a Delta table in OCSF format. Each event is routed to the appropriate
# OCSF event class based on the operationName and category:
#
#   - API Activity (6003)     — default for resource operations
#   - Authentication (3002)   — sign-in events (operationName contains "Sign"
#                               or category is a sign-in log type)
#   - Account Change (3001)   — RBAC role assignment/definition changes
#
# All three classes are stored in a single table with class_uid as a column,
# matching the pattern used by the AWS CloudTrail notebook. The original
# Activity Log event is preserved in the raw_data field for forensic/audit use.
#
# Source format: JSON under insights-activity-logs/ (Azure diagnostic settings
#   export Activity Log to this system-managed path structure)
# Target table: security_poc.bronze.activity_log_raw
# OCSF version: 1.1.0
#
# Parameters (passed via job or widgets):
#   - azure_workload_a_storage_url: Storage URL for Azure workload A
#                                   (e.g. abfss://container@account.dfs.core.windows.net/)
#   - checkpoint_base:              Path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_azure_common

# COMMAND ----------

dbutils.widgets.text("azure_workload_a_storage_url", "", "Azure Workload A Storage URL")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

azure_workload_a_storage_url = dbutils.widgets.get("azure_workload_a_storage_url")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source path — Azure diagnostic settings export Activity Log JSON files under
# the insights-activity-logs/ prefix. storage_url already includes the scheme
# and trailing slash (e.g. "abfss://container@account.dfs.core.windows.net/"),
# so path suffixes are appended directly.
source_paths = {
    "azure_workload_a": f"{azure_workload_a_storage_url}insights-activity-logs/",
}

checkpoint_base_al = f"{checkpoint_base}/activity_log"
target_table = "security_poc.bronze.activity_log_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_al}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, current_timestamp, to_json, struct, lit, when, array, upper
)

# =============================================================================
# OCSF CLASS ROUTING — determines which OCSF class an Activity Log event
# maps to based on operationName and category fields
# =============================================================================

def _is_auth_event(operation_col, category_col):
    """
    Returns a boolean column that is True when the event should be classified
    as an Authentication event (OCSF class 3002).

    Checks two conditions:
      1. operationName contains any of the AUTH_OPERATION_PATTERNS substrings
      2. category matches any of the AUTH_CATEGORIES values
    """
    # Build OR chain for operationName pattern matching (case-insensitive)
    op_upper = upper(operation_col)
    auth_condition = F.lit(False)
    for pattern in AUTH_OPERATION_PATTERNS:
        auth_condition = auth_condition | op_upper.contains(pattern.upper())

    # Check if category is in the known sign-in log categories
    category_condition = category_col.isin(list(AUTH_CATEGORIES))

    return auth_condition | category_condition


def _is_account_change_event(operation_col):
    """
    Returns a boolean column that is True when the event should be classified
    as an Account Change event (OCSF class 3001).

    Checks if operationName starts with any of the ACCOUNT_CHANGE_OPERATION_PATTERNS
    (RBAC role assignments, role definitions, elevated access).
    """
    op_upper = upper(operation_col)
    condition = F.lit(False)
    for pattern in ACCOUNT_CHANGE_OPERATION_PATTERNS:
        condition = condition | op_upper.startswith(pattern.upper())
    return condition

# COMMAND ----------

# =============================================================================
# OCSF TRANSFORMATION — Activity Log -> OCSF API Activity / Authentication /
#                                        Account Change
# =============================================================================
# Reads the Auto Loader inferred schema, classifies each event into an OCSF
# class, then maps fields to the OCSF structure. The full original event is
# preserved as a JSON string in raw_data.

def transform_activity_log_to_ocsf(df):
    """
    Transform a raw Azure Activity Log DataFrame (one row per event, after
    optional records explosion) into OCSF v1.1.0 format.
    """

    # ── Step 1: Route to OCSF class based on operationName and category ─────
    df_classified = df.withColumn(
        "class_uid",
        when(
            _is_auth_event(col("operationName"), col("category")),
            lit(CLASS_AUTHENTICATION)
        )
        .when(
            _is_account_change_event(col("operationName")),
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

    # ── Step 2: Derive activity_id and status ───────────────────────────────
    # Azure Activity Log uses resultType for success/failure indication.
    # Common values: "Success", "Start", "Accept" -> success; "Failure" -> failure.
    df_activity = df_classified.withColumn(
        "activity_id", azure_activity_id(col("operationName"))
    ).withColumn(
        "status_id",
        when(upper(col("resultType")) == "SUCCESS", lit(STATUS_SUCCESS))
        .when(upper(col("resultType")) == "START", lit(STATUS_SUCCESS))
        .when(upper(col("resultType")) == "ACCEPT", lit(STATUS_SUCCESS))
        .when(upper(col("resultType")) == "FAILURE", lit(STATUS_FAILURE))
        .otherwise(lit(STATUS_UNKNOWN))
        .cast("int")
    ).withColumn(
        "status",
        when(upper(col("resultType")) == "SUCCESS", lit("Success"))
        .when(upper(col("resultType")) == "START", lit("Success"))
        .when(upper(col("resultType")) == "ACCEPT", lit("Success"))
        .when(upper(col("resultType")) == "FAILURE", lit("Failure"))
        .otherwise(col("resultType"))
    )

    # ── Step 3: Extract subscription ID from resourceId ─────────────────────
    # Azure resourceId format: /subscriptions/{sub}/resourceGroups/{rg}/...
    # We extract the subscription GUID for the cloud.account.uid field.
    df_with_sub = df_activity.withColumn(
        "_subscription_id",
        F.element_at(F.split(col("resourceId"), "/"), 3)
    )

    # ── Step 4: Build OCSF columns ─────────────────────────────────────────
    df_ocsf = df_with_sub.select(
        # ── Classification ──
        col("class_uid"),
        col("category_uid"),
        col("activity_id"),
        compute_type_uid(col("class_uid"), col("activity_id")).alias("type_uid"),
        azure_level_to_severity(col("level")).alias("severity_id"),
        severity_label(azure_level_to_severity(col("level"))).alias("severity"),

        # ── Time ──
        # Activity Log 'time' field is ISO8601 string — cast to timestamp.
        # Fall back to 'eventTimestamp' if 'time' is not present.
        F.coalesce(
            col("time").cast("timestamp"),
            col("eventTimestamp").cast("timestamp"),
        ).alias("time"),

        # ── Status ──
        col("status_id"),
        col("status"),
        col("resultSignature").alias("status_code"),
        col("resultType").alias("status_detail"),

        # ── API object — the core of Activity Log mapping ──
        F.struct(
            col("operationName").alias("operation"),
            F.struct(
                col("category").alias("name"),
            ).alias("service"),
        ).alias("api"),

        # ── Actor — who performed the action ──
        # Activity Log uses 'caller' (UPN or object ID) and 'claims' for
        # identity details. The caller field may be an email (user UPN) or
        # a GUID (service principal object ID).
        F.struct(
            F.struct(
                col("caller").alias("name"),
                col("caller").alias("uid"),
                # Determine type: if caller looks like email -> User, else SP
                when(
                    col("caller").contains("@"),
                    lit("User")
                ).otherwise(lit("ServicePrincipal")).alias("type"),
            ).alias("user"),
        ).alias("actor"),

        # ── Resource — the target of the operation ──
        # Parse the Azure resource ID into structured components.
        when(
            col("resourceId").isNotNull(),
            parse_azure_resource_id(col("resourceId"))
        ).alias("resource"),

        # ── Cloud context ──
        # Use extracted subscription ID; Azure Activity Log does not always
        # include a region field, so we use "global" as a fallback.
        ocsf_cloud(
            F.coalesce(col("location"), lit("global")),
            F.coalesce(col("_subscription_id"), lit("unknown"))
        ).alias("cloud"),

        # ── Metadata ──
        ocsf_metadata("Activity Log", "Activity Log").alias("metadata"),

        # ── Unmapped — fields that don't fit OCSF cleanly ──
        # properties is a complex nested JSON blob that varies per operation.
        # Storing it as a JSON string in unmapped preserves the data without
        # imposing a schema.
        F.map_from_arrays(
            array(
                lit("properties"),
                lit("correlation_id"),
                lit("resource_id"),
                lit("event_data_id"),
                lit("operation_name"),
            ),
            array(
                to_json(col("properties")).cast("string"),
                col("correlationId").cast("string"),
                col("resourceId").cast("string"),
                col("eventDataId").cast("string"),
                col("operationName").cast("string"),
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
# Auto Loader reads raw Activity Log JSON, then the OCSF transformation is
# applied. Azure diagnostic settings export Activity Log as JSON with a
# top-level "records" array.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_al}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read raw Activity Log JSON with schema inference.
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Azure diagnostic settings JSON files contain a top-level "records"
        # array. If the inferred schema has a records column, explode it to
        # get one row per event. If Auto Loader already flattened it, skip.
        if "records" in raw_df.columns:
            raw_df = raw_df.select(F.explode("records").alias("_record")).select("_record.*")
        elif "Records" in raw_df.columns:
            raw_df = raw_df.select(F.explode("Records").alias("_record")).select("_record.*")

        # Apply OCSF transformation and write to Delta.
        ocsf_df = transform_activity_log_to_ocsf(raw_df)

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

print(f"Activity Log ingestion complete. Rows: {spark.table(target_table).count()}")
