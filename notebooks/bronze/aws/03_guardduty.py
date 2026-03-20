# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: GuardDuty Findings Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests raw GuardDuty findings from workload account S3 buckets and writes
# them to a Delta table in OCSF Detection Finding (class_uid 2004) format.
#
# GuardDuty exports findings as JSONL (one JSON object per line, gzipped).
# Unlike the Amazon Security Lake path (GuardDuty → Security Hub ASFF → OCSF),
# this notebook maps directly from the raw GuardDuty finding schema to OCSF.
# The mapping covers core fields; the variable-structure service.action and
# resource details are preserved in the unmapped field and raw_data.
#
# Key mapping decisions:
#   - GuardDuty severity (0-10 float) → OCSF severity_id (0-5) using
#     Amazon Security Lake thresholds: <4=Low, <7=Medium, <9=High, >=9=Critical
#   - service.action is too variable per finding type to fully normalize —
#     stored as JSON in unmapped
#   - activity_id=1 (Create) for all findings (GuardDuty exports are new/updated)
#
# Source format: JSONL (gzipped) under AWSLogs/{account}/GuardDuty/
# Target table: security_poc.bronze.guardduty
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

# Source paths — GuardDuty findings are under the AWSLogs/ prefix at the bucket
# root. The pathGlobFilter ensures we only pick up .jsonl.gz files (GuardDuty)
# and not .json.gz (CloudTrail/Config). storage_url already includes the scheme
# and trailing slash (e.g. "s3://bucket/"), so path suffixes are appended directly.
source_paths = {
    "workload_a": f"{workload_a_storage_url}AWSLogs/",
    "workload_b": f"{workload_b_storage_url}AWSLogs/",
}

checkpoint_base_gd = f"{checkpoint_base}/guardduty"
target_table = "security_poc.bronze.guardduty"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_gd}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, lit, when, to_json, struct, array

# =============================================================================
# OCSF TRANSFORMATION — GuardDuty → OCSF Detection Finding (2004)
# =============================================================================
# GuardDuty raw finding fields:
#   id, type, title, description, severity (float 0-10),
#   accountId, region, createdAt, updatedAt,
#   resource (variable structure per resourceType),
#   service (contains action, evidence, count, etc.)
#
# The mapping focuses on the stable top-level fields. Resource details and
# service.action vary per finding type and are preserved in unmapped/raw_data.

def transform_guardduty_to_ocsf(df):
    """
    Transform a raw GuardDuty findings DataFrame into OCSF Detection Finding.
    """

    # Derive OCSF severity from GuardDuty's 0-10 scale.
    severity_id_col = guardduty_severity_to_ocsf(col("severity"))

    df_ocsf = df.select(
        # ── Classification ──
        lit(CLASS_DETECTION_FINDING).cast("int").alias("class_uid"),
        lit(CATEGORY_FINDINGS).cast("int").alias("category_uid"),
        lit(ACTIVITY_FINDING_CREATE).cast("int").alias("activity_id"),
        lit(CLASS_DETECTION_FINDING * 100 + ACTIVITY_FINDING_CREATE).cast("long").alias("type_uid"),
        severity_id_col.alias("severity_id"),
        severity_label(severity_id_col).alias("severity"),

        # ── Time — updatedAt is the most recent event time ──
        col("updatedAt").cast("timestamp").alias("time"),

        # ── Status — GuardDuty findings are always active when exported ──
        lit(STATUS_SUCCESS).cast("int").alias("status_id"),
        lit("Success").alias("status"),

        # ── Finding info — core identification and description ──
        F.struct(
            col("id").alias("uid"),
            col("title").alias("title"),
            col("description").alias("desc"),
            F.array(col("type")).alias("types"),
            col("createdAt").cast("timestamp").alias("created_time"),
            col("updatedAt").cast("timestamp").alias("modified_time"),
        ).alias("finding_info"),

        # ── Resources — the AWS resource(s) involved in the finding ──
        # GuardDuty's resource object varies by resourceType (Instance,
        # AccessKey, S3Bucket, etc.). We extract the type and preserve the
        # full resource object as a JSON string in the data field.
        F.array(
            F.struct(
                F.get_json_object(to_json(col("resource")), "$.resourceType").alias("type"),
                to_json(col("resource")).alias("data"),
            )
        ).alias("resources"),

        # ── Cloud context ──
        ocsf_cloud(col("region"), col("accountId")).alias("cloud"),

        # ── Metadata ──
        F.struct(
            lit(OCSF_VERSION).alias("version"),
            F.struct(
                lit("AWS").alias("vendor_name"),
                lit("GuardDuty").alias("name"),
                F.struct(
                    F.get_json_object(to_json(col("service")), "$.serviceName").alias("name"),
                ).alias("feature"),
            ).alias("product"),
            lit("GuardDuty Findings").alias("log_name"),
        ).alias("metadata"),

        # ── Unmapped — variable-structure fields ──
        # service.action varies per finding type (AwsApiCallAction,
        # NetworkConnectionAction, DnsRequestAction, etc.). Rather than
        # trying to normalize all variants, preserve as JSON.
        # Note: Not all findings have every service sub-field (e.g., evidence
        # is absent from many finding types). Since missing fields cause
        # analysis errors even with getField(), we use get_json_object on the
        # serialized service struct to safely extract optional fields.
        F.map_from_arrays(
            array(
                lit("service_action"),
                lit("service_evidence"),
                lit("service_count"),
                lit("service_detector_id"),
                lit("confidence"),
                lit("resource_type"),
            ),
            array(
                F.get_json_object(to_json(col("service")), "$.action").cast("string"),
                F.get_json_object(to_json(col("service")), "$.evidence").cast("string"),
                F.get_json_object(to_json(col("service")), "$.count").cast("string"),
                F.get_json_object(to_json(col("service")), "$.detectorId").cast("string"),
                # confidence may not exist at the top level in all GuardDuty exports
                F.lit(None).cast("string"),
                F.get_json_object(to_json(col("resource")), "$.resourceType").cast("string"),
            ),
        ).alias("unmapped"),

        # ── Raw data — complete original finding as JSON ──
        to_json(struct(df.columns)).alias("raw_data"),

        # ── Ingestion metadata (project convention) ──
        current_timestamp().alias("_ingested_at"),
        col("_metadata.file_path").alias("_source_file"),
    )

    return df_ocsf

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — process each workload account sequentially
# =============================================================================

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_gd}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .option("pathGlobFilter", "*.jsonl.gz")
            .load(path)
        )

        # Apply OCSF transformation.
        ocsf_df = transform_guardduty_to_ocsf(raw_df)

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
        # GuardDuty may not have exported findings yet for all accounts.
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no GuardDuty files found yet.")
        else:
            raise

print(f"GuardDuty ingestion complete. Rows: {spark.table(target_table).count()}")
