# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Layer: AWS Config CDC (Change Data Capture)
# -----------------------------------------------------------------------------
# Reads raw AWS Config data from bronze.config_raw, explodes the
# configurationItems array, and writes a normalized CDC table that tracks
# every configuration change to every AWS resource across all accounts.
#
# AWS Config delivers two file types:
#   - ConfigHistory: per-resource change history (configurationItems array)
#   - ConfigSnapshot: full inventory snapshots (configurationItems array)
#
# Both contain the same configurationItems structure, so a single
# transformation handles both. Each configurationItem represents one
# point-in-time configuration state for one resource.
#
# CDC semantics come from configurationItemStatus:
#   - ResourceDiscovered: first time Config sees the resource (INSERT)
#   - OK:                 configuration changed since last capture (UPDATE)
#   - ResourceDeleted:    resource was deleted (DELETE)
#   - ResourceNotRecorded: resource exists but Config doesn't record it
#
# The composite key is:
#   (aws_account_id, resource_type, resource_id, capture_time)
#
# Source table: security_poc.bronze.config_raw
# Target table: security_poc.silver.config_cdc
#
# Parameters (passed via job or widgets):
#   - checkpoint_base: S3 path for streaming checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

source_table = "security_poc.bronze.config_raw"
target_table = "security_poc.silver.config_cdc"
checkpoint_location = f"{checkpoint_base}/silver/config_cdc"

print(f"Source: {source_table}")
print(f"Target: {target_table}")
print(f"Checkpoint: {checkpoint_location}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, current_timestamp, lit, when, to_json

# =============================================================================
# TRANSFORMATION — Explode configurationItems and extract CDC columns
# =============================================================================
# The bronze table contains raw Config JSON files. Each file has a
# configurationItems array (ConfigHistory) or a top-level structure
# (ConfigSnapshot). We handle both by checking for the array column.
#
# Extracted columns fall into three categories:
#   1. Identity: resource_type, resource_id, arn, resource_name
#   2. Context: aws_account_id, aws_region, availability_zone, capture_time
#   3. State: status (CDC operation), configuration, supplementary_configuration,
#             relationships, tags, resource_creation_time
#
# Variable-structure fields (configuration, supplementary_configuration,
# relationships) are kept as JSON strings — the gold layer parses them
# for specific resource types (e.g., EC2 instances).

def transform_config_to_cdc(df):
    """
    Transform a raw Config DataFrame into a normalized CDC table.
    Expects the DataFrame to have a configurationItems array column.
    """

    # Explode the configurationItems array — one row per resource change.
    df_exploded = df.select(
        F.explode("configurationItems").alias("item"),
        col("_ingested_at").alias("_bronze_ingested_at"),
        col("_source_file"),
    )

    # Extract and normalize fields from each configuration item.
    df_cdc = df_exploded.select(
        # ── Identity — what resource changed ──
        col("item.resourceType").alias("resource_type"),
        col("item.resourceId").alias("resource_id"),
        col("item.ARN").alias("arn"),
        col("item.resourceName").alias("resource_name"),

        # ── Context — where and when ──
        col("item.awsAccountId").alias("aws_account_id"),
        col("item.awsRegion").alias("aws_region"),
        col("item.availabilityZone").alias("availability_zone"),
        col("item.configurationItemCaptureTime").cast("timestamp").alias("capture_time"),

        # ── CDC status — what kind of change ──
        # Maps Config's status values to a human-readable change_type.
        col("item.configurationItemStatus").alias("status"),
        when(col("item.configurationItemStatus") == "ResourceDiscovered", lit("INSERT"))
        .when(col("item.configurationItemStatus") == "OK", lit("UPDATE"))
        .when(col("item.configurationItemStatus") == "ResourceDeleted", lit("DELETE"))
        .when(col("item.configurationItemStatus") == "ResourceNotRecorded", lit("NOT_RECORDED"))
        .otherwise(lit("UNKNOWN"))
        .alias("change_type"),

        # ── Configuration state — the resource's configuration at capture time ──
        # Kept as JSON strings because structure varies per resource type.
        # Gold layer notebooks parse these for specific resource types.
        to_json(col("item.configuration")).alias("configuration"),
        to_json(col("item.supplementaryConfiguration")).alias("supplementary_configuration"),

        # ── Relationships — how this resource connects to others ──
        # Critical for gold-layer joins (e.g., EC2 → ENI → SG → VPC).
        # Array of {resourceType, resourceId, resourceName, name} objects.
        to_json(col("item.relationships")).alias("relationships"),

        # ── Tags — resource tags as a JSON object ──
        to_json(col("item.tags")).alias("tags"),

        # ── Resource creation time — when the resource was first created ──
        col("item.resourceCreationTime").cast("timestamp").alias("resource_creation_time"),

        # ── Config metadata — version and status of the Config item itself ──
        col("item.configurationItemVersion").alias("config_item_version"),
        col("item.configurationStateId").alias("config_state_id"),

        # ── Ingestion metadata ──
        current_timestamp().alias("_ingested_at"),
        col("_source_file"),
        col("_bronze_ingested_at"),
    )

    return df_cdc

# COMMAND ----------

# =============================================================================
# STREAMING READ — read new rows from bronze, transform, write to silver
# =============================================================================
# Uses Delta streaming (readStream from a Delta table) to incrementally
# process only new bronze rows since the last checkpoint. This is more
# efficient than a full table scan and guarantees exactly-once processing.
#
# The bronze table may contain rows without configurationItems (e.g.,
# malformed files or non-standard Config output). We filter these out
# before exploding.

raw_df = (
    spark.readStream
    .table(source_table)
)

# Filter to rows that have a configurationItems array — this handles
# both ConfigHistory and ConfigSnapshot file types. Rows without this
# column (if any) are skipped.
has_items = raw_df.filter(col("configurationItems").isNotNull())

# Apply the CDC transformation.
cdc_df = transform_config_to_cdc(has_items)

# Write to the silver CDC table. Uses append mode since each bronze row
# produces new CDC records (we never update existing silver rows).
# mergeSchema handles any new fields that appear in future Config versions.
(
    cdc_df.writeStream
    .format("delta")
    .outputMode("append")
    .option("checkpointLocation", checkpoint_location)
    .option("mergeSchema", "true")
    .trigger(availableNow=True)
    .toTable(target_table)
)

print(f"Config CDC complete. Rows: {spark.table(target_table).count()}")

# COMMAND ----------

# =============================================================================
# VALIDATION — show sample data and key metrics
# =============================================================================
# Quick summary to verify the CDC table looks correct after each run.

cdc = spark.table(target_table)

print(f"\nTotal CDC rows: {cdc.count()}")
print(f"\nRows by change_type:")
cdc.groupBy("change_type").count().orderBy("count", ascending=False).show()

print(f"\nRows by resource_type:")
cdc.groupBy("resource_type").count().orderBy("count", ascending=False).show(50, truncate=False)

print(f"\nRows by account:")
cdc.groupBy("aws_account_id").count().show()

print(f"\nSample rows:")
cdc.select(
    "aws_account_id", "resource_type", "resource_id",
    "capture_time", "change_type", "status"
).orderBy("capture_time", ascending=False).show(10, truncate=False)
