# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: CloudTrail Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests CloudTrail management events from workload account S3 buckets and
# writes them to a Delta table in OCSF format. Each event is routed to the
# appropriate OCSF event class based on the eventName:
#
#   - API Activity (6003)     — default for most CloudTrail events
#   - Authentication (3002)   — ConsoleLogin, AssumeRole, STS token events
#   - Account Change (3001)   — IAM user/role/group/policy mutations
#
# All three classes are stored in a single table with class_uid as a column,
# matching the pattern Amazon Security Lake uses. The original CloudTrail
# event is preserved in the raw_data field for forensic/audit use.
#
# Source format: JSON (gzipped) under cloudtrail/AWSLogs/
# Target table: security_poc.bronze.cloudtrail
# OCSF version: 1.1.0
#
# Parameters (passed via job or widgets):
#   - workload_a_storage_url: Storage URL for workload account A (e.g. s3://bucket/)
#   - workload_b_storage_url: Storage URL for workload account B (e.g. s3://bucket/)
#   - checkpoint_base:        S3 path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_ocsf_common

# COMMAND ----------

dbutils.widgets.text("workload_a_storage_url", "", "Workload A Storage URL")
dbutils.widgets.text("workload_b_storage_url", "", "Workload B Storage URL")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

workload_a_storage_url = dbutils.widgets.get("workload_a_storage_url")
workload_b_storage_url = dbutils.widgets.get("workload_b_storage_url")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source paths — one per workload account. CloudTrail writes JSON files under
# the cloudtrail/AWSLogs/ prefix. Auto Loader recursively discovers all .json.gz
# files under these paths. storage_url already includes the scheme and trailing
# slash (e.g. "s3://bucket/"), so path suffixes are appended directly.
source_paths = {
    "workload_a": f"{workload_a_storage_url}cloudtrail/AWSLogs/",
    "workload_b": f"{workload_b_storage_url}cloudtrail/AWSLogs/",
}

checkpoint_base_ct = f"{checkpoint_base}/cloudtrail"
target_table = "security_poc.bronze.cloudtrail"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_ct}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, to_json, struct, lit, when, array

# =============================================================================
# OCSF TRANSFORMATION — CloudTrail → OCSF API Activity / Authentication /
#                                       Account Change
# =============================================================================
# The transformation reads the Auto Loader inferred schema, then selects and
# renames fields into the OCSF structure. Fields that don't have a clean OCSF
# mapping go into the unmapped map. The full original event is preserved as
# a JSON string in raw_data.

def transform_cloudtrail_to_ocsf(df):
    """
    Transform a raw CloudTrail DataFrame (one row per event, after Records
    explosion) into OCSF v1.1.0 format.
    """

    # ── Step 1: Route to OCSF class based on eventName ──────────────────────
    # Broadcast the routing sets as Spark-friendly column expressions.
    auth_events = list(AUTH_EVENT_NAMES)
    acct_events = list(ACCOUNT_CHANGE_EVENT_NAMES)

    df_classified = df.withColumn(
        "class_uid",
        when(col("eventName").isin(auth_events), lit(CLASS_AUTHENTICATION))
        .when(col("eventName").isin(acct_events), lit(CLASS_ACCOUNT_CHANGE))
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
    df_activity = df_classified.withColumn(
        "activity_id", cloudtrail_activity_id(col("readOnly"))
    ).withColumn(
        "status_id",
        when(col("errorCode").isNull(), lit(STATUS_SUCCESS))
        .otherwise(lit(STATUS_FAILURE))
        .cast("int")
    ).withColumn(
        "status",
        when(col("errorCode").isNull(), lit("Success"))
        .otherwise(lit("Failure"))
    )

    # ── Step 3: Build OCSF columns ─────────────────────────────────────────
    df_ocsf = df_activity.select(
        # ── Classification ──
        col("class_uid"),
        col("category_uid"),
        col("activity_id"),
        compute_type_uid(col("class_uid"), col("activity_id")).alias("type_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),
        lit("Informational").alias("severity"),

        # ── Time ──
        # CloudTrail eventTime is ISO8601 string — cast to timestamp.
        col("eventTime").cast("timestamp").alias("time"),

        # ── Status ──
        col("status_id"),
        col("status"),
        col("errorCode").alias("status_code"),
        col("errorMessage").alias("status_detail"),

        # ── API object — the core of CloudTrail mapping ──
        F.struct(
            col("eventName").alias("operation"),
            F.struct(
                col("eventSource").alias("name"),
            ).alias("service"),
        ).alias("api"),

        # ── Actor — who performed the action ──
        # CloudTrail userIdentity is a complex nested object. We extract the
        # most important fields into the OCSF actor struct.
        # Note: userName is not always present in the schema (e.g., for
        # assumed-role sessions, AWS services, or federated users). Since the
        # field may be entirely absent from the inferred schema, we use
        # get_json_object on the serialized struct to safely extract it.
        F.struct(
            F.struct(
                col("userIdentity.arn").alias("uid"),
                F.coalesce(
                    F.get_json_object(to_json(col("userIdentity")), "$.userName"),
                    col("userIdentity.principalId"),
                ).alias("name"),
                col("userIdentity.type").alias("type"),
                col("userIdentity.principalId").alias("credential_uid"),
                F.struct(
                    col("userIdentity.accountId").cast("string").alias("uid"),
                ).alias("account"),
            ).alias("user"),
            F.struct(
                col("userIdentity.sessionContext.sessionIssuer.arn").alias("issuer"),
            ).alias("session"),
        ).alias("actor"),

        # ── Source endpoint — where the API call originated ──
        # sourceIPAddress can be an IP or an AWS service name like
        # "cloudtrail.amazonaws.com". We populate ip for IPs and domain otherwise.
        F.struct(
            when(
                col("sourceIPAddress").rlike(r"^\d+\.\d+\.\d+\.\d+$"),
                col("sourceIPAddress")
            ).alias("ip"),
            when(
                ~col("sourceIPAddress").rlike(r"^\d+\.\d+\.\d+\.\d+$"),
                col("sourceIPAddress")
            ).alias("domain"),
        ).alias("src_endpoint"),

        # ── HTTP request metadata ──
        F.struct(
            col("userAgent").alias("user_agent"),
        ).alias("http_request"),

        # ── Cloud context ──
        ocsf_cloud(col("awsRegion"), col("recipientAccountId")).alias("cloud"),

        # ── Metadata ──
        ocsf_metadata("CloudTrail", "Management Events").alias("metadata"),

        # ── Unmapped — fields that don't fit OCSF cleanly ──
        # requestParameters and responseElements are deeply nested JSON blobs
        # that vary per API call. Storing them as JSON strings in unmapped
        # preserves the data without imposing a schema.
        F.map_from_arrays(
            array(
                lit("request_parameters"),
                lit("response_elements"),
                lit("event_type"),
                lit("event_category"),
                lit("management_event"),
            ),
            array(
                to_json(col("requestParameters")).cast("string"),
                to_json(col("responseElements")).cast("string"),
                col("eventType").cast("string"),
                col("eventCategory").cast("string"),
                col("managementEvent").cast("string"),
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
# INGESTION LOOP — process each workload account sequentially
# =============================================================================
# Each account gets its own Auto Loader stream with a dedicated checkpoint.
# Auto Loader reads raw CloudTrail JSON, then the OCSF transformation is
# applied via foreachBatch so we can transform within the streaming context.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_ct}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read raw CloudTrail JSON with schema inference.
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # CloudTrail JSON files contain a top-level "Records" array. If the
        # inferred schema has a Records column, explode it to get one row per
        # event. If Auto Loader already flattened it, skip the explode.
        if "Records" in raw_df.columns:
            raw_df = raw_df.select(F.explode("Records").alias("_record")).select("_record.*")

        # Apply OCSF transformation and write to Delta.
        ocsf_df = transform_cloudtrail_to_ocsf(raw_df)

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

print(f"CloudTrail ingestion complete. Rows: {spark.table(target_table).count()}")
