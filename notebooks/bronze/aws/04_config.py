# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: AWS Config Ingestion
# -----------------------------------------------------------------------------
# Ingests raw AWS Config snapshots and history from workload account S3 buckets
# into a Delta table using Auto Loader (cloudFiles format).
#
# AWS Config writes two types of files:
#   - ConfigHistory: per-resource change history files
#   - ConfigSnapshot: full configuration snapshots (if delivery channel has
#     snapshot delivery configured — not enabled in Phase 4)
#
# Source format: JSON (gzipped) under config/AWSLogs/{account}/Config/
# Target table: security_poc.bronze.config_raw
#
# Parameters (passed via job or widgets):
#   - workload_a_bucket: S3 bucket name for workload account A
#   - workload_b_bucket: S3 bucket name for workload account B
#   - checkpoint_base:   S3 path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

dbutils.widgets.text("workload_a_bucket", "", "Workload A Bucket")
dbutils.widgets.text("workload_b_bucket", "", "Workload B Bucket")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

workload_a_bucket = dbutils.widgets.get("workload_a_bucket")
workload_b_bucket = dbutils.widgets.get("workload_b_bucket")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source paths — AWS Config writes under the config/ prefix with the standard
# AWSLogs/{account}/Config/{region}/ structure.
# Each workload account gets its own stream and checkpoint.
source_paths = {
    "workload_a": f"s3://{workload_a_bucket}/config/AWSLogs/",
    "workload_b": f"s3://{workload_b_bucket}/config/AWSLogs/",
}

checkpoint_base_cfg = f"{checkpoint_base}/config"
target_table = "security_poc.bronze.config_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_cfg}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, col

# Process each workload account sequentially with its own checkpoint.
for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_cfg}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .option("pathGlobFilter", "*.json.gz")
            .load(path)
        )

        # Unity Catalog does not support input_file_name() — use _metadata.file_path.
        df_enriched = df.withColumn(
            "_ingested_at", current_timestamp()
        ).withColumn(
            "_source_file", col("_metadata.file_path")
        )

        (
            df_enriched.writeStream
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

print(f"Config ingestion complete. Rows: {spark.table(target_table).count()}")
